"""The one execution backend: a local Docker daemon reached through an isolated client.

`open_sandbox` resolves the explicitly configured Docker context to a local
Unix socket through Docker's own context metadata, then talks to that socket
with an empty, private client configuration and a minimal environment, so no
ambient `DOCKER_HOST`, context, proxy, registry credential or debug setting
reaches the daemon or a container. It verifies that the configured concrete
image exists locally and was built from this package's recipe; it never
pulls, builds or installs anything, and there is no other backend to fall
back to.

Every execution gets its own uniquely named container: no network, read-only
root, all capabilities dropped, no new privileges, bounded CPU, memory (swap
included), processes, open files and wall time, and bounded `noexec` tmpfs
scratch and input (and render output). Nothing from the host is mounted. The
container's main process is a keeper at a different UID from the generated
code, so the code cannot stop it: the container ends itself at its hard
lifetime and is then removed automatically. Inputs are written by a trusted
stager over stdin into keeper-owned files the code can read but not change;
output streams are read incrementally up to a byte limit; source commands
start behind a trusted launcher whose start marker separates Docker failing
to start them (an infrastructure fault) from anything the program does (its
own result, whatever its exit status or output); render output is copied by
a trusted descriptor-walking collector while the container is alive, once
the kernel's process count shows no source process left. Whatever happens,
the whole container is force-removed and its absence confirmed by the daemon
before a result is returned or derived bytes are written; failure to confirm
is an infrastructure error, never a pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import struct
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apprentice.controls.credentials import require_generation_environment
from apprentice.sandbox.errors import SandboxInfrastructureError, SandboxUnavailableError
from apprentice.sandbox.programs import (
    COLLECTOR_REJECTED,
    INPUT_DIR,
    KEEPER_USER,
    OUTPUT_DIR,
    RENDER_FPS,
    RENDER_HEIGHT,
    RENDER_WIDTH,
    SCENE_FILE,
    SCRATCH_DIR,
    SOURCE_USER,
    START_MARKER,
    VIDEO_FILE,
    collector,
    keeper,
    media_probe_command,
    program_command,
    render_command,
    stager,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from apprentice.core.config import ExecutionConfig

RECIPE_DIR = Path(__file__).parent / "image"
RECIPE_FILES = ("Dockerfile", "requirements.in", "requirements.lock")
RECIPE_LABEL = "ai.no-magic.apprentice.sandbox-recipe"
SANDBOX_LABEL = "ai.no-magic.apprentice.sandbox"
SCOPE_LABEL = "ai.no-magic.apprentice.scope"
EXPIRES_LABEL = "ai.no-magic.apprentice.expires"
CONTAINER_PREFIX = "apprentice-sandbox-"

MAX_VIDEO_BYTES = 16 << 20
MAX_VIDEO_SECONDS = 600

_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_CONTEXT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}")
_RUNTIME_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_CONTROL_SECONDS = 60
_CONTROL_OUTPUT = 1 << 20
_TEARDOWN_SECONDS = 60
_EXCERPT = 400
# Tasks of the keeper itself (docker-init and the sleeping keeper process).
_KEEPER_TASKS = 2


@dataclass(frozen=True)
class Profile:
    """Fixed resource bounds of one kind of contained execution."""

    name: str
    cpus: str
    memory_mb: int
    pids: int
    lifetime_seconds: int
    program_seconds: int
    output_bytes: int
    scratch_mb: int
    scratch_inodes: int
    input_mb: int
    render_mb: int = 0
    render_inodes: int = 0


# A generated implementation: stdlib Python, its own exit status.
PROGRAM = Profile(
    name="program",
    cpus="1",
    memory_mb=512,
    pids=64,
    lifetime_seconds=60,
    program_seconds=5,
    output_bytes=64 << 10,
    scratch_mb=64,
    scratch_inodes=4096,
    input_mb=2,
)
# A generated Manim scene rendered to a small preview video.
SCENE = Profile(
    name="scene",
    cpus="2",
    memory_mb=1024,
    pids=128,
    lifetime_seconds=300,
    program_seconds=180,
    output_bytes=256 << 10,
    scratch_mb=128,
    scratch_inodes=16384,
    input_mb=2,
    render_mb=128,
    render_inodes=16384,
)
# The probe of an already collected, immutable preview video.
MEDIA = Profile(
    name="media",
    cpus="1",
    memory_mb=256,
    pids=32,
    lifetime_seconds=90,
    program_seconds=30,
    output_bytes=64 << 10,
    scratch_mb=16,
    scratch_inodes=256,
    input_mb=32,
)


def recipe_digest() -> str:
    """Digest of the image recipe: SHA-256 of `shasum -a 256 Dockerfile requirements.in requirements.lock`."""
    listing = "".join(
        f"{hashlib.sha256((RECIPE_DIR / name).read_bytes()).hexdigest()}  {name}\n"
        for name in RECIPE_FILES
    )
    return hashlib.sha256(listing.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProgramRun:
    """Outcome of one contained program: exit status, or the limit that stopped it.

    `output` is the combined stdout and stderr, at most the profile's byte
    limit. `limit` is "time" or "output" when that limit ended the program
    (the container was destroyed with every process in it).
    """

    returncode: int | None
    output: str
    limit: str | None
    profile: Profile

    @property
    def passed(self) -> bool:
        return self.limit is None and self.returncode == 0

    def problem(self) -> str:
        """Why the program did not pass, or "" when it did."""
        if self.limit == "time":
            return f"timed out after {self.profile.program_seconds}s"
        if self.limit == "output":
            return f"wrote more than {self.profile.output_bytes} bytes of output"
        if self.returncode is not None and self.returncode > 128:
            return (
                f"was killed by signal {self.returncode - 128} (memory limit "
                f"{self.profile.memory_mb} MB or process limit {self.profile.pids})"
            )
        if self.returncode:
            return f"exited with status {self.returncode}"
        return ""


@dataclass(frozen=True)
class SceneRender:
    """Outcome of rendering one scene: verified preview bytes, or why there are none."""

    problem: str
    output: str
    video: bytes = b""
    probe: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.problem


@dataclass
class _Client:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    truncated: bool
    timed_out: bool
    interrupted: bool


@dataclass
class _Box:
    name: str
    profile: Profile
    id: str = ""


def _excerpt(data: bytes) -> str:
    return data.decode("utf-8", "replace").strip()[:_EXCERPT]


def _run_client(
    argv: list[str],
    env: dict[str, str],
    *,
    stdin: bytes | None,
    timeout: float,
    limit: int,
    merge: bool,
    shield: bool,
) -> _Client:
    """Run one Docker client in its own session, reading at most `limit` bytes per stream.

    The client never shares the terminal's process group, so Ctrl-C reaches
    only apprentice. When `shield` is set an interruption is remembered and
    the call still completes (it is reported as `interrupted`); otherwise the
    client is killed and the interruption propagates. A client still running
    at `timeout`, or whose output passed `limit`, is killed.

    Raises:
        SandboxUnavailableError: If the Docker client cannot be started.
    """
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge else subprocess.PIPE,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        raise SandboxUnavailableError(f"the docker client cannot be started: {exc}") from exc
    assert proc.stdout is not None
    out_buffer = bytearray()
    err_buffer = bytearray()
    buffers: dict[int, bytearray] = {proc.stdout.fileno(): out_buffer}
    if proc.stderr is not None:
        buffers[proc.stderr.fileno()] = err_buffer
    selector = selectors.DefaultSelector()
    for fd in buffers:
        os.set_blocking(fd, False)
        selector.register(fd, selectors.EVENT_READ)
    pending = memoryview(stdin or b"")
    stdin_fd = -1
    if proc.stdin is not None:
        stdin_fd = proc.stdin.fileno()
        if pending:
            os.set_blocking(stdin_fd, False)
            selector.register(stdin_fd, selectors.EVENT_WRITE)
        else:
            proc.stdin.close()
    deadline = time.monotonic() + timeout
    truncated = timed_out = interrupted = False
    try:
        while selector.get_map() and not truncated:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                events = selector.select(remaining)
            except KeyboardInterrupt:
                if not shield:
                    raise
                interrupted = True
                continue
            for key, _ in events:
                if key.fd == stdin_fd:
                    try:
                        written = os.write(stdin_fd, pending[: 1 << 16])
                    except BrokenPipeError:
                        written = len(pending)
                    pending = pending[written:]
                    if not pending:
                        selector.unregister(stdin_fd)
                        assert proc.stdin is not None
                        proc.stdin.close()
                    continue
                chunk = os.read(key.fd, 1 << 16)
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                buffer = buffers[key.fd]
                room = limit - len(buffer)
                buffer.extend(chunk[:room])
                if len(chunk) > room:
                    truncated = True
                    break
        if not (truncated or timed_out):
            while True:
                try:
                    proc.wait(max(0.0, deadline - time.monotonic()))
                    break
                except subprocess.TimeoutExpired:
                    timed_out = True
                    break
                except KeyboardInterrupt:
                    if not shield:
                        raise
                    interrupted = True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        selector.close()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()
    return _Client(
        returncode=None if (truncated or timed_out) else proc.returncode,
        stdout=bytes(out_buffer),
        stderr=bytes(err_buffer),
        truncated=truncated,
        timed_out=timed_out,
        interrupted=interrupted,
    )


def _context_endpoint(docker: str, context: str) -> str:
    """Resolve `context` to its local Unix socket from Docker's context metadata."""
    env = {"PATH": _RUNTIME_PATH, "HOME": str(Path.home())}
    if os.environ.get("DOCKER_CONFIG"):
        env["DOCKER_CONFIG"] = os.environ["DOCKER_CONFIG"]
    result = _run_client(
        [docker, "context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}"],
        env,
        stdin=None,
        timeout=_CONTROL_SECONDS,
        limit=_CONTROL_OUTPUT,
        merge=False,
        shield=False,
    )
    if result.returncode != 0:
        raise SandboxUnavailableError(
            f"Docker context {context!r} cannot be resolved: {_excerpt(result.stderr)}"
        )
    try:
        endpoint = json.loads(result.stdout)
    except ValueError as exc:
        raise SandboxUnavailableError(f"Docker context {context!r} has no endpoint") from exc
    if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
        raise SandboxUnavailableError(
            f"Docker context {context!r} endpoint {endpoint!r} is not a local Unix socket; "
            "only a local Docker daemon is supported"
        )
    socket_path = endpoint.removeprefix("unix://")
    try:
        info = os.stat(socket_path)
    except OSError as exc:
        raise SandboxUnavailableError(
            f"Docker context {context!r} socket {socket_path} is unavailable: {exc.strerror}"
        ) from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise SandboxUnavailableError(f"Docker context {context!r} endpoint is not a socket")
    return endpoint


def open_sandbox(config: ExecutionConfig) -> Sandbox:
    """Verify the configured backend and return a sandbox bound to it (close it after use).

    Raises:
        SandboxUnavailableError: If no backend is configured, the Docker CLI,
            context, daemon or image is missing or unsupported, or the image
            was not built from this package's recipe. Nothing is executed.
        PublicationCredentialError: If a known publication credential is
            present in the environment; validation never runs next to one.
    """
    require_generation_environment()
    if not config.docker_context or not config.image:
        raise SandboxUnavailableError(
            "no execution backend is configured: set [execution].docker_context and "
            "[execution].image (docs/configuration.md#execution); generated code never "
            "runs on the host"
        )
    if not _CONTEXT_NAME.fullmatch(config.docker_context):
        raise SandboxUnavailableError(f"unsupported Docker context name {config.docker_context!r}")
    if not _IMAGE_ID.fullmatch(config.image):
        raise SandboxUnavailableError(
            f"[execution].image {config.image!r} is not a concrete image ID (sha256:<64 hex>)"
        )
    docker = shutil.which("docker")
    if docker is None:
        raise SandboxUnavailableError("the docker CLI is not installed or not on PATH")
    endpoint = _context_endpoint(docker, config.docker_context)
    sandbox = Sandbox(docker=docker, endpoint=endpoint, image=config.image)
    try:
        sandbox.verify_image()
    except BaseException:
        sandbox.close()
        raise
    return sandbox


class Sandbox:
    """A verified local Docker backend that runs generated code in disposable containers."""

    def __init__(self, *, docker: str, endpoint: str, image: str) -> None:
        self._config = tempfile.TemporaryDirectory(prefix="apprentice-docker-")
        self._prefix = [docker, "--config", self._config.name, "--host", endpoint]
        self._env = {"PATH": _RUNTIME_PATH, "HOME": self._config.name}
        self.endpoint = endpoint
        self.image = image

    def close(self) -> None:
        """Remove the private client configuration."""
        self._config.cleanup()

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _control(self, args: list[str], *, shield: bool = False, timeout: float = 0) -> _Client:
        return _run_client(
            [*self._prefix, *args],
            self._env,
            stdin=None,
            timeout=timeout or _CONTROL_SECONDS,
            limit=_CONTROL_OUTPUT,
            merge=False,
            shield=shield,
        )

    def verify_image(self) -> None:
        """Require the configured image locally, built from this recipe, running as the source UID.

        Raises:
            SandboxUnavailableError: If the daemon or image is unavailable or unsupported.
        """
        result = self._control(["image", "inspect", "--format", "{{json .}}", self.image])
        if result.returncode != 0:
            if b"No such image" in result.stderr:
                raise SandboxUnavailableError(
                    f"sandbox image {self.image} is not present on {self.endpoint}; build it "
                    "as described in docs/configuration.md#execution (nothing is pulled)"
                )
            raise SandboxUnavailableError(
                f"Docker daemon at {self.endpoint} is unavailable: {_excerpt(result.stderr)}"
            )
        try:
            image = json.loads(result.stdout)
            identity = image["Id"]
            system = image["Os"]
            user = image["Config"]["User"]
            labels = image["Config"]["Labels"] or {}
        except (ValueError, KeyError, TypeError) as exc:
            raise SandboxUnavailableError(f"cannot read sandbox image {self.image}") from exc
        if identity != self.image or system != "linux" or user != SOURCE_USER:
            raise SandboxUnavailableError(
                f"image {self.image} is not a supported sandbox image (id {identity!r}, "
                f"os {system!r}, user {user!r})"
            )
        expected = recipe_digest()
        if labels.get(RECIPE_LABEL) != expected:
            raise SandboxUnavailableError(
                f"image {self.image} was built from recipe {labels.get(RECIPE_LABEL)!r}, not "
                f"the current recipe {expected}; rebuild it (docs/configuration.md#execution)"
            )

    # --- container lifecycle -------------------------------------------------

    def _run_args(self, box: _Box, scope: str) -> list[str]:
        profile = box.profile
        expires = int(time.time()) + profile.lifetime_seconds
        tmpfs = "rw,noexec,nosuid,nodev"
        args = [
            "run",
            "--detach",
            "--rm",
            "--init",
            "--name",
            box.name,
            "--label",
            f"{SANDBOX_LABEL}=1",
            "--label",
            f"{SCOPE_LABEL}={scope}",
            "--label",
            f"{EXPIRES_LABEL}={expires}",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            KEEPER_USER,
            "--workdir",
            "/",
            "--hostname",
            "sandbox",
            "--cpus",
            profile.cpus,
            "--memory",
            f"{profile.memory_mb}m",
            "--memory-swap",
            f"{profile.memory_mb}m",
            "--pids-limit",
            str(profile.pids),
            "--ulimit",
            "core=0",
            "--ulimit",
            "nofile=1024:1024",
            "--log-driver",
            "none",
            "--tmpfs",
            f"{SCRATCH_DIR}:{tmpfs},size={profile.scratch_mb}m,"
            f"nr_inodes={profile.scratch_inodes},mode=1777",
            "--tmpfs",
            f"{INPUT_DIR}:{tmpfs},size={profile.input_mb}m,nr_inodes=16,"
            "uid=65531,gid=65531,mode=755",
        ]
        if profile.render_mb:
            args += [
                "--tmpfs",
                f"{OUTPUT_DIR}:{tmpfs},size={profile.render_mb}m,"
                f"nr_inodes={profile.render_inodes},uid=65532,gid=65532,mode=755",
            ]
        return [*args, self.image, *keeper(profile.lifetime_seconds)]

    @contextmanager
    def _container(self, profile: Profile, scope: str) -> Iterator[_Box]:
        """Run a fresh keeper container for the block; always remove it and confirm its absence."""
        self._recover(scope)
        box = _Box(name=f"{CONTAINER_PREFIX}{uuid.uuid4().hex}", profile=profile)
        try:
            started = self._control(self._run_args(box, scope))
            identity = started.stdout.decode("ascii", "replace").strip()
            if started.returncode != 0 or not _CONTAINER_ID.fullmatch(identity):
                raise SandboxInfrastructureError(
                    f"cannot start a sandbox container: {_excerpt(started.stderr)}"
                )
            box.id = identity
            yield box
        finally:
            self._teardown(box)

    def _recover(self, scope: str) -> None:
        """Remove expired leftovers of this scope; refuse while another one is still alive."""
        listing = self._control(
            [
                "ps",
                "--all",
                "--no-trunc",
                "--filter",
                f"label={SCOPE_LABEL}={scope}",
                "--format",
                '{{.Names}}\t{{.Label "' + EXPIRES_LABEL + '"}}',
            ]
        )
        if listing.returncode != 0:
            raise SandboxInfrastructureError(
                f"Docker daemon at {self.endpoint} is unavailable: {_excerpt(listing.stderr)}"
            )
        now = int(time.time())
        for line in listing.stdout.decode("utf-8", "replace").splitlines():
            name, _, expires = line.partition("\t")
            if not name.startswith(CONTAINER_PREFIX):
                continue
            if expires.isdigit() and int(expires) < now:
                self._teardown(_Box(name=name, profile=PROGRAM))
                continue
            raise SandboxInfrastructureError(
                f"sandbox container {name} of this artifact root is still alive (an earlier "
                f"execution that ended abnormally, or a concurrent one); it expires at "
                f"{expires or 'an unknown time'} (UNIX time) and is then removed automatically"
            )

    def _teardown(self, box: _Box) -> None:
        """Force-remove the whole container and confirm with the daemon that it is gone.

        An operator stop (SIGTERM) waits until this is done; a Ctrl-C during it
        is held and raised afterwards. The keeper's hard lifetime with
        automatic removal bounds the container even if this process dies.

        Raises:
            SandboxInfrastructureError: If the absence cannot be confirmed in time.
            KeyboardInterrupt: An interruption that arrived during the teardown.
        """
        from apprentice.core.cycles import recording_end

        interrupted: KeyboardInterrupt | None = None
        failure: SandboxUnavailableError | None = None
        confirmed = False
        with recording_end():
            deadline = time.monotonic() + _TEARDOWN_SECONDS
            while not confirmed and time.monotonic() < deadline:
                try:
                    removal = self._control(["rm", "--force", box.name], shield=True, timeout=30)
                    # Absence is only proven by a daemon that answered and listed nothing.
                    listing = self._control(
                        ["ps", "--all", "--quiet", "--no-trunc", "--filter", f"name={box.name}"],
                        shield=True,
                        timeout=30,
                    )
                    if removal.interrupted or listing.interrupted:
                        interrupted = interrupted or KeyboardInterrupt()
                    confirmed = listing.returncode == 0 and not listing.stdout.strip()
                    if not confirmed:
                        time.sleep(0.5)
                except KeyboardInterrupt as exc:
                    interrupted = exc
                except SandboxUnavailableError as exc:
                    failure = exc
                    break
        if not confirmed:
            raise SandboxInfrastructureError(
                f"sandbox container {box.name} could not be confirmed removed; it ends at its "
                "hard lifetime, but this execution does not count as passed"
            ) from (failure or interrupted)
        if interrupted is not None:
            raise interrupted

    # --- steps inside a container -------------------------------------------

    def _exec(
        self,
        box: _Box,
        user: str,
        command: list[str],
        *,
        workdir: str,
        timeout: float,
        limit: int,
        merge: bool,
        stdin: bytes | None = None,
    ) -> _Client:
        args = ["exec", "--user", user, "--workdir", workdir]
        if stdin is not None:
            args.append("--interactive")
        return _run_client(
            [*self._prefix, *args, box.id, *command],
            self._env,
            stdin=stdin,
            timeout=timeout,
            limit=limit,
            merge=merge,
            shield=False,
        )

    def _stage(self, box: _Box, files: dict[str, bytes]) -> None:
        frame = bytearray(struct.pack(">I", len(files)))
        for name, data in files.items():
            encoded = name.encode("ascii")
            frame += struct.pack(">H", len(encoded)) + encoded + struct.pack(">Q", len(data)) + data
        result = self._exec(
            box,
            KEEPER_USER,
            stager(),
            workdir="/",
            timeout=60,
            limit=4096,
            merge=False,
            stdin=bytes(frame),
        )
        if result.returncode != 0 or result.stdout != f"staged {len(files)}\n".encode():
            raise SandboxInfrastructureError(
                f"staging inputs into the sandbox failed: {_excerpt(result.stderr)}"
            )

    def _require_running(self, box: _Box) -> None:
        state = self._control(["container", "inspect", "--format", "{{.State.Running}}", box.id])
        if state.returncode != 0 or state.stdout.strip() != b"true":
            raise SandboxInfrastructureError(
                f"sandbox container {box.name} stopped or became unreachable while the "
                f"program ran: {_excerpt(state.stderr)}"
            )

    def _run_source(self, box: _Box, command: list[str]) -> ProgramRun:
        """Run a `launched` source command and return its contained outcome.

        Only the launcher's start marker, which precedes anything the command
        can write, shows that Docker started it; from then on the exit status
        and output, whatever they are (Docker's own failure statuses or error
        texts included), are the program's result. The marker is not counted
        against the output limit.

        Raises:
            SandboxInfrastructureError: If there is no start marker (Docker could
                not start the launcher) or the container stopped meanwhile.
        """
        profile = box.profile
        result = self._exec(
            box,
            SOURCE_USER,
            command,
            workdir=SCRATCH_DIR,
            timeout=profile.program_seconds,
            limit=profile.output_bytes + len(START_MARKER),
            merge=True,
        )
        if not result.stdout.startswith(START_MARKER):
            state = "timed out" if result.timed_out else f"exited with status {result.returncode}"
            raise SandboxInfrastructureError(
                f"the sandbox could not start the program (docker exec {state} before the "
                f"launcher started): {_excerpt(result.stdout)}"
            )
        output = result.stdout[len(START_MARKER) :].decode("utf-8", "replace")
        if result.timed_out:
            return ProgramRun(None, output, "time", profile)
        if result.truncated:
            return ProgramRun(None, output, "output", profile)
        if result.returncode != 0:
            self._require_running(box)
        return ProgramRun(result.returncode, output, None, profile)

    def _live_source_tasks(self, box: _Box) -> int:
        """Count the tasks the source user left, from the kernel's count for the container.

        The count is the container's cgroup task count (`docker stats`), read
        from outside the container, so it needs no process inside it and is
        not affected by a source that exhausted the process limit. Every task
        beyond the keeper's own belongs to the source.

        Raises:
            SandboxInfrastructureError: If the count cannot be read or shows
                that the keeper itself is gone.
        """
        result = self._control(
            ["stats", "--no-stream", "--no-trunc", "--format", "{{.PIDs}}", box.id]
        )
        count = result.stdout.strip()
        if result.returncode != 0 or not count.isdigit() or int(count) < _KEEPER_TASKS:
            raise SandboxInfrastructureError(
                f"cannot count the processes of sandbox container {box.name} "
                f"({count.decode('ascii', 'replace')!r}): {_excerpt(result.stderr)}"
            )
        return int(count) - _KEEPER_TASKS

    def _collect(self, box: _Box) -> bytes | str:
        """Return the rendered video's bytes, or why the render output is unacceptable."""
        result = self._exec(
            box,
            KEEPER_USER,
            collector(MAX_VIDEO_BYTES),
            workdir="/",
            timeout=60,
            limit=MAX_VIDEO_BYTES + 41,
            merge=False,
        )
        if result.returncode == COLLECTOR_REJECTED and result.stdout.startswith(b"reject: "):
            return _excerpt(result.stdout.removeprefix(b"reject: "))
        frame = result.stdout
        if result.returncode != 0 or len(frame) < 40:
            raise SandboxInfrastructureError(
                f"collecting the rendered video failed: {_excerpt(result.stderr)}"
            )
        (size,) = struct.unpack(">Q", frame[:8])
        data, digest = frame[8:-32], frame[-32:]
        if size != len(data) or size > MAX_VIDEO_BYTES or hashlib.sha256(data).digest() != digest:
            raise SandboxInfrastructureError("the collected video failed its integrity check")
        return data

    # --- public executions ---------------------------------------------------

    def run_program(self, source: bytes, *, scope: str) -> ProgramRun:
        """Run a generated Python program (`python -I`) and return its contained outcome.

        Raises:
            SandboxInfrastructureError: If the backend cannot run it or remove its container.
        """
        with self._container(PROGRAM, scope) as box:
            self._stage(box, {"implementation.py": source})
            return self._run_source(box, program_command())

    def render_scene(self, source: bytes, scene_class: str, *, scope: str) -> SceneRender:
        """Import, construct and render `scene_class`, then verify the collected preview video.

        The video is copied out while the render container is alive and is
        then probed, as immutable staged bytes, in a fresh container; nothing
        the scene wrote is parsed on the host or by the keeper.

        Raises:
            SandboxInfrastructureError: If the backend cannot run the render,
                collect its output or remove a container.
        """
        with self._container(SCENE, scope) as box:
            self._stage(box, {SCENE_FILE: source})
            run = self._run_source(box, render_command(scene_class))
            if not run.passed:
                return SceneRender(problem=f"the scene render {run.problem()}", output=run.output)
            live = self._live_source_tasks(box)
            if live:
                # A source task can only come from a live one, so none can appear later.
                return SceneRender(
                    problem=(
                        f"the scene render left {live} processes or threads running "
                        f"(the container allows {box.profile.pids} tasks)"
                    ),
                    output=run.output,
                )
            video = self._collect(box)
            if isinstance(video, str):
                if video.startswith("no rendered video"):
                    # Manim saves only an image for a scene that never animates.
                    video += "; the scene must play at least one animation or wait"
                return SceneRender(problem=video, output=run.output)
        problem, probe = self._probe(video, scope)
        if problem:
            return SceneRender(problem=problem, output=run.output, probe=probe)
        return SceneRender(problem="", output=run.output, video=video, probe=probe)

    def _probe(self, video: bytes, scope: str) -> tuple[str, dict[str, Any]]:
        with self._container(MEDIA, scope) as box:
            self._stage(box, {VIDEO_FILE: video})
            run = self._run_source(box, media_probe_command())
        if not run.passed:
            return f"the rendered video failed the media check: it {run.problem()}", {}
        try:
            probe = json.loads(run.output)
            (stream,) = probe["streams"]
            container = probe["format"]
            frames = int(stream["nb_read_frames"])
            duration = float(container["duration"])
            size = int(container["size"])
            facts = {
                "codec": stream["codec_name"],
                "width": stream["width"],
                "height": stream["height"],
                "frames": frames,
                "duration": duration,
                "size": size,
            }
        except (ValueError, KeyError, TypeError):
            return "the rendered video is not a readable single-stream video", {}
        expected = {
            "codec": "h264",
            "width": RENDER_WIDTH,
            "height": RENDER_HEIGHT,
            "size": len(video),
        }
        mismatched = sorted(key for key, value in expected.items() if facts[key] != value)
        if mismatched or not 1 <= frames <= RENDER_FPS * MAX_VIDEO_SECONDS:
            return f"the rendered video has unexpected {mismatched or ['frame count']}", facts
        if not 0 < duration <= MAX_VIDEO_SECONDS:
            return f"the rendered video lasts {duration}s; at most {MAX_VIDEO_SECONDS}s", facts
        return "", facts
