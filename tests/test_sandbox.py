"""Containment of generated code in the real Docker sandbox, plus its fail-closed classification.

The containment cases need a local Docker daemon and the operator-built
sandbox image; they run when APPRENTICE_TEST_DOCKER_CONTEXT and
APPRENTICE_TEST_SANDBOX_IMAGE name them and are skipped (visibly) otherwise.
Every probe is a harmless program written here, never model output. The
configuration and start-evidence cases run without a daemon.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import pwd
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from apprentice.agents.implementation import _make_after_drafter_callback
from apprentice.core.config import ExecutionConfig
from apprentice.sandbox.docker import SCOPE_LABEL, open_sandbox
from apprentice.sandbox.errors import SandboxInfrastructureError, SandboxUnavailableError
from apprentice.sandbox.programs import START_MARKER
from apprentice.sandbox.staging import scope_label
from apprentice.validators.tools import ValidatorTools

if TYPE_CHECKING:
    from collections.abc import Iterator

    from apprentice.sandbox.docker import Sandbox

_CONTEXT = os.environ.get("APPRENTICE_TEST_DOCKER_CONTEXT", "")
_IMAGE = os.environ.get("APPRENTICE_TEST_SANDBOX_IMAGE", "")
_REAL_DOCKER_CONFIG = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".docker"

needs_docker = pytest.mark.skipif(
    not (_CONTEXT and _IMAGE),
    reason="set APPRENTICE_TEST_DOCKER_CONTEXT and APPRENTICE_TEST_SANDBOX_IMAGE to run containment",
)


class TestMissingBackend:
    @pytest.mark.parametrize(
        ("context", "image"),
        [("", ""), ("desktop-linux", ""), ("", "sha256:" + "0" * 64)],
    )
    def test_unconfigured_backend_is_refused(self, context: str, image: str) -> None:
        with pytest.raises(SandboxUnavailableError, match="no execution backend is configured"):
            open_sandbox(ExecutionConfig(docker_context=context, image=image))

    def test_image_must_be_a_concrete_id(self) -> None:
        with pytest.raises(SandboxUnavailableError, match="not a concrete image ID"):
            open_sandbox(ExecutionConfig(docker_context="default", image="python:3.13"))

    def test_without_a_docker_cli_nothing_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        with pytest.raises(SandboxUnavailableError, match="docker CLI"):
            open_sandbox(ExecutionConfig(docker_context="default", image="sha256:" + "0" * 64))


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sandbox]:
    # The context metadata lives in the real user's Docker configuration, not the private HOME.
    monkeypatch.setenv("DOCKER_CONFIG", str(_REAL_DOCKER_CONFIG))
    with open_sandbox(ExecutionConfig(docker_context=_CONTEXT, image=_IMAGE)) as opened:
        yield opened


@pytest.fixture
def scope(tmp_path: Path, sandbox: Sandbox) -> Iterator[str]:
    root = tmp_path / "root"
    root.mkdir()
    label = scope_label(root)
    yield label
    left = subprocess.run(
        [
            "docker",
            "--context",
            _CONTEXT,
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label={SCOPE_LABEL}={label}",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "DOCKER_CONFIG": str(_REAL_DOCKER_CONFIG)},
    ).stdout
    assert left == "", "a sandbox container outlived its execution"


def _program(body: str) -> bytes:
    return textwrap.dedent(body).encode()


@needs_docker
class TestContainment:
    def test_program_sees_no_host_file_secret_network_or_writable_input(
        self, sandbox: Sandbox, scope: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = tmp_path / "sentinel.txt"
        sentinel.write_text("host sentinel")
        monkeypatch.setenv("GH_TOKEN", "fixture_" + "s" * 32)
        probe = _program(
            f"""
            import json, os, signal, socket
            facts = {{"uid": os.getuid(), "sentinel": os.path.exists({str(sentinel)!r})}}
            facts["env"] = sorted(os.environ)
            for target in ("/input/implementation.py", "/etc/escape", "/escape"):
                try:
                    open(target, "a").close()
                    facts[target] = "writable"
                except OSError as exc:
                    facts[target] = type(exc).__name__
            try:
                socket.create_connection(("1.1.1.1", 53), timeout=2).close()
                facts["network"] = "reachable"
            except OSError as exc:
                facts["network"] = type(exc).__name__
            try:
                os.kill(1, signal.SIGKILL)
                facts["keeper"] = "killed"
            except OSError as exc:
                facts["keeper"] = type(exc).__name__
            with open("/proc/self/status") as status:
                facts["cap_eff"] = next(
                    line.split()[1] for line in status if line.startswith("CapEff")
                )
            print(json.dumps(facts))
            """
        )

        run = sandbox.run_program(probe, scope=scope)

        assert run.passed, run.output
        facts = json.loads(run.output)
        assert facts == {
            "uid": 65532,
            "sentinel": False,
            "env": [
                "GPG_KEY",
                "HOME",
                "HOSTNAME",
                "LC_CTYPE",
                "MPLCONFIGDIR",
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "PATH",
                "PYTHONDONTWRITEBYTECODE",
                "PYTHON_SHA256",
                "PYTHON_VERSION",
            ],
            "/input/implementation.py": "PermissionError",
            "/etc/escape": "OSError",
            "/escape": "OSError",
            "network": "OSError",
            "keeper": "PermissionError",
            "cap_eff": "0000000000000000",
        }

    def test_time_limit_destroys_the_program_and_its_children(
        self, sandbox: Sandbox, scope: str
    ) -> None:
        probe = _program(
            """
            import os, time
            if os.fork() == 0:
                os.setsid()
                time.sleep(3600)
            time.sleep(3600)
            """
        )

        run = sandbox.run_program(probe, scope=scope)

        assert (run.passed, run.limit) == (False, "time")

    def test_background_child_of_a_passing_program_does_not_outlive_it(
        self, sandbox: Sandbox, scope: str
    ) -> None:
        probe = _program(
            """
            import os, time
            if os.fork() == 0:
                os.setsid()
                time.sleep(3600)
            print("parent done")
            """
        )

        run = sandbox.run_program(probe, scope=scope)

        assert run.passed
        # The scope fixture asserts the whole container, with the child, is gone.

    def test_output_flood_is_cut_at_the_limit(self, sandbox: Sandbox, scope: str) -> None:
        probe = _program(
            """
            import sys
            while True:
                sys.stdout.write("x" * 65536)
                sys.stderr.write("y" * 65536)
            """
        )

        run = sandbox.run_program(probe, scope=scope)

        assert run.limit == "output"
        assert len(run.output) == run.profile.output_bytes

    def test_memory_and_process_limits_bound_the_program(
        self, sandbox: Sandbox, scope: str
    ) -> None:
        probe = _program(
            """
            import os, time
            children = 0
            try:
                for _ in range(500):
                    if os.fork() == 0:
                        time.sleep(60)
                        os._exit(0)
                    children += 1
            except OSError:
                pass
            print(children, flush=True)
            blob = bytearray(2 << 30)
            """
        )

        run = sandbox.run_program(probe, scope=scope)

        assert not run.passed
        assert int(run.output.split()[0]) < run.profile.pids

    @pytest.mark.parametrize(
        ("ending", "status"),
        [
            ("sys.exit(125)", 125),
            ("sys.exit(126)", 126),
            ("sys.exit(127)", 127),
            ("os._exit(126)", 126),
        ],
    )
    def test_docker_like_exit_status_is_the_programs_own_failure(
        self, sandbox: Sandbox, scope: str, ending: str, status: int
    ) -> None:
        probe = _program(
            f"""
            import os, sys
            sys.stdout.write("drafted output\\n")
            sys.stdout.flush()
            {ending}
            """
        )

        run = sandbox.run_program(probe, scope=scope)

        assert (run.passed, run.returncode, run.limit) == (False, status, None)
        assert run.output == "drafted output\n"

    @pytest.mark.parametrize("stream", ["stdout", "stderr"])
    def test_docker_like_error_text_is_the_programs_own_output(
        self, sandbox: Sandbox, scope: str, stream: str
    ) -> None:
        text = "Error response from daemon: OCI runtime exec failed: exec failed\n"
        probe = _program(
            f"""
            import sys
            sys.{stream}.write({text!r})
            sys.exit(127)
            """
        )

        run = sandbox.run_program(probe, scope=scope)

        assert (run.passed, run.returncode, run.output) == (False, 127, text)

    @pytest.mark.parametrize(("size", "limit"), [(65536, None), (65537, "output")])
    def test_output_limit_counts_only_the_programs_bytes(
        self, sandbox: Sandbox, scope: str, size: int, limit: str | None
    ) -> None:
        probe = _program(
            f"""
            import sys
            sys.stdout.buffer.write((bytes(range(256)) * 257)[:{size}])
            """
        )

        run = sandbox.run_program(probe, scope=scope)

        assert run.limit == limit
        assert run.passed is (limit is None)

    def test_drafter_gets_feedback_for_a_docker_like_failure_instead_of_an_abort(
        self, sandbox: Sandbox, scope: str, tmp_path: Path
    ) -> None:
        root = tmp_path / "root"
        draft = (
            'if __name__ == "__main__":\n'
            "    import sys\n"
            '    print("Error response from daemon: no such container")\n'
            "    sys.exit(126)\n"
        )
        context = SimpleNamespace(state={"generated_code": draft})

        asyncio.run(_make_after_drafter_callback(ValidatorTools(root, sandbox))(context))

        assert context.state["implementation_passed"] is False
        feedback = context.state["validation_feedback"]
        assert "exited with status 126" in feedback
        assert "Error response from daemon: no such container" in feedback


_SCENE = b"""from manim import MathTex, Scene, Text, VGroup, Write


class Probe(Scene):
    def construct(self):
        self.play(Write(VGroup(Text("probe"), MathTex(r"e^{i\\pi}+1=0")).arrange()))
"""


@needs_docker
class TestSceneRender:
    def test_scene_renders_to_a_verified_preview_video(self, sandbox: Sandbox, scope: str) -> None:
        render = sandbox.render_scene(_SCENE, "Probe", scope=scope)

        assert render.passed, render.problem + render.output
        assert render.video.startswith(b"\x00\x00\x00") and b"ftyp" in render.video[:16]
        assert render.probe["codec"] == "h264"
        assert (render.probe["width"], render.probe["height"]) == (320, 180)
        assert render.probe["size"] == len(render.video)

    def test_scene_error_is_a_failed_render_not_an_infrastructure_fault(
        self, sandbox: Sandbox, scope: str
    ) -> None:
        broken = _SCENE.replace(b"self.play(", b"raise RuntimeError('scene bug') or self.play(")

        render = sandbox.render_scene(broken, "Probe", scope=scope)

        assert not render.passed
        assert "scene bug" in render.output

    def test_static_scene_renders_no_video_and_fails(self, sandbox: Sandbox, scope: str) -> None:
        static = _SCENE.replace(b"self.play(Write(", b"self.add((")

        render = sandbox.render_scene(static, "Probe", scope=scope)

        assert render.problem.startswith("no rendered video")
        assert render.problem.endswith("the scene must play at least one animation or wait")

    def test_render_that_leaves_processes_running_is_rejected(
        self, sandbox: Sandbox, scope: str
    ) -> None:
        lingering = _SCENE.replace(
            b"    def construct(self):\n",
            b"    def construct(self):\n"
            b"        import os, time\n"
            b"        if os.fork() == 0:\n"
            b"            os.setsid()\n"
            b"            time.sleep(3600)\n",
        )

        render = sandbox.render_scene(lingering, "Probe", scope=scope)

        assert render.problem.startswith("the scene render left 1 processes or threads running")
        assert render.video == b""

    def test_scene_that_exhausts_the_process_limit_is_a_failed_render(
        self, sandbox: Sandbox, scope: str
    ) -> None:
        saturating = (
            _SCENE
            + b"""

class Saturating(Probe):
    def construct(self):
        super().construct()
        import os, time
        try:
            while True:
                if os.fork() == 0:
                    time.sleep(3600)
                    os._exit(0)
        except OSError:
            pass
"""
        )

        render = sandbox.render_scene(saturating, "Saturating", scope=scope)

        assert not render.passed
        assert render.problem.startswith("the scene render ")
        assert render.video == b""

    def test_linked_render_output_is_never_collected(self, sandbox: Sandbox, scope: str) -> None:
        linked = _SCENE.replace(
            b"    def construct(self):\n",
            b"    def construct(self):\n"
            b"        import os\n"
            b"        os.makedirs('/output/videos/scene/180p5', exist_ok=True)\n"
            b"        os.symlink('/tmp/elsewhere.mp4', '/output/videos/scene/180p5/preview.mp4')\n",
        )

        render = sandbox.render_scene(linked, "Probe", scope=scope)

        assert not render.passed
        assert render.video == b""


_FAKE_DOCKER = """#!{python}
import base64, json, sys
scenario = json.load(open({scenario!r}))
args = sys.argv[5:]  # after --config DIR --host ENDPOINT
out = sys.stdout.buffer
if args[0] == "run":
    out.write(b"f" * 64 + b"\\n")
elif args[0] == "exec" and "--interactive" in args:
    frame = sys.stdin.buffer.read()
    out.write(b"staged %d\\n" % int.from_bytes(frame[:4], "big"))
elif args[0] == "exec":
    out.write(base64.b64decode(scenario["exec_output"]))
    out.flush()
    sys.exit(scenario["exec_status"])
elif args[0] == "stats":
    out.write(scenario["pids"].encode())
    sys.exit(scenario["stats_status"])
elif args[0] == "container":
    out.write(b"true\\n")
"""


class _FakeClient:
    """A Docker client stand-in that reports chosen exec outcomes and task counts.

    It never runs anything: it lets the classification of what Docker reports
    be checked without a daemon. The real backend is covered by the
    `needs_docker` cases above.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.scenario = tmp_path / "scenario.json"
        self.executable = tmp_path / "docker"
        self.executable.write_text(
            _FAKE_DOCKER.format(python=sys.executable, scenario=str(self.scenario))
        )
        self.executable.chmod(0o755)
        self.root = tmp_path / "root"
        self.root.mkdir()

    def sandbox(
        self, output: bytes, status: int = 0, *, pids: str = "2\n", stats_status: int = 0
    ) -> Sandbox:
        from apprentice.sandbox.docker import Sandbox

        self.scenario.write_text(
            json.dumps(
                {
                    "exec_output": base64.b64encode(output).decode(),
                    "exec_status": status,
                    "pids": pids,
                    "stats_status": stats_status,
                }
            )
        )
        return Sandbox(
            docker=str(self.executable), endpoint="unix:///fake.sock", image="sha256:" + "0" * 64
        )


@pytest.fixture
def fake(tmp_path: Path) -> _FakeClient:
    return _FakeClient(tmp_path)


class TestStartEvidence:
    """Only the launcher's start marker, never the program's status or output, means Docker started it."""

    @pytest.mark.parametrize("status", [125, 126, 127])
    def test_after_the_marker_docker_like_status_and_text_are_the_programs_result(
        self, fake: _FakeClient, status: int
    ) -> None:
        text = b"Error response from daemon: OCI runtime exec failed\n"

        run = fake.sandbox(START_MARKER + text, status).run_program(b"", scope="s")

        assert (run.passed, run.returncode, run.limit, run.output) == (
            False,
            status,
            None,
            text.decode(),
        )

    @pytest.mark.parametrize(
        ("output", "status"),
        [
            (b"OCI runtime exec failed: exec: python: not found\n", 127),
            (b"Error response from daemon: container is not running\n", 1),
            (b"", 0),
        ],
    )
    def test_without_the_marker_docker_did_not_start_the_program(
        self, fake: _FakeClient, output: bytes, status: int
    ) -> None:
        with pytest.raises(SandboxInfrastructureError, match="could not start the program"):
            fake.sandbox(output, status).run_program(b"", scope="s")

    @pytest.mark.parametrize(("size", "limit"), [(65536, None), (65537, "output")])
    def test_the_marker_does_not_count_against_the_output_limit(
        self, fake: _FakeClient, size: int, limit: str | None
    ) -> None:
        run = fake.sandbox(START_MARKER + b"x" * size).run_program(b"", scope="s")

        assert run.limit == limit
        assert len(run.output) == min(size, run.profile.output_bytes)

    def test_drafter_gets_feedback_for_a_docker_like_status(self, fake: _FakeClient) -> None:
        sandbox = fake.sandbox(START_MARKER + b"Error response from daemon: gone\n", 126)
        context = SimpleNamespace(
            state={"generated_code": 'if __name__ == "__main__":\n    pass\n'}
        )

        asyncio.run(_make_after_drafter_callback(ValidatorTools(fake.root, sandbox))(context))

        assert context.state["implementation_passed"] is False
        assert "exited with status 126" in context.state["validation_feedback"]

    def test_tasks_left_by_the_scene_fail_the_render(self, fake: _FakeClient) -> None:
        render = fake.sandbox(START_MARKER, pids="5\n").render_scene(b"", "Probe", scope="s")

        assert render.problem.startswith("the scene render left 3 processes or threads running")
        assert render.video == b""

    @pytest.mark.parametrize(
        ("pids", "stats_status"), [("--\n", 0), ("1\n", 0), ("2\n", 1), ("", 0)]
    )
    def test_an_unknown_task_count_never_passes(
        self, fake: _FakeClient, pids: str, stats_status: int
    ) -> None:
        sandbox = fake.sandbox(START_MARKER, pids=pids, stats_status=stats_status)

        with pytest.raises(SandboxInfrastructureError, match="cannot count the processes"):
            sandbox.render_scene(b"", "Probe", scope="s")
