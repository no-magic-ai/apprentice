"""Shared fixtures: store-owned run scopes, trusted role outputs and offline packaging remotes."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from apprentice.core.session_store import SessionStore

if TYPE_CHECKING:
    from apprentice.core.artifacts import RunScope


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own empty HOME and no hosted-provider environment.

    The default store/log roots and the installation footprint then never
    see the real user's `~/.apprentice`, and no hosted credential or base URL
    reaches a route.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def judged_execution(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace generated-code execution in validators and gates with a non-executing judge.

    Model output (even from the loopback fixture) is never run: the judge
    reads the file and passes it unless it contains the fixture's deliberate
    failure marker. Returns the judged paths in order.
    """
    judged: list[str] = []

    def judge(
        args: list[str], *positional: Any, **keyword: Any
    ) -> subprocess.CompletedProcess[str]:
        path = Path(args[-1])
        judged.append(str(path))
        failing = "fixture: deliberate failure" in path.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(
            args, 1 if failing else 0, "", "AssertionError: fixture" if failing else ""
        )

    judged_subprocess = SimpleNamespace(run=judge, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr("apprentice.validators.correctness.subprocess", judged_subprocess)
    monkeypatch.setattr("apprentice.gates.correctness.subprocess", judged_subprocess)
    return judged


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(store_dir=tmp_path / "sessions")


@pytest.fixture
def scope(store: SessionStore) -> RunScope:
    return store.run_scope(store.create_run("selection", 2))


_PASSING_IMPLEMENTATION = '''"""Selection sort proves that repeatedly selecting the minimum yields a sorted list.

Args:
    values: Integers to sort.

Returns:
    A new list with the same integers in ascending order.

Complexity:
    Time: O(n^2) comparisons in every case.
    Space: O(n) for the returned copy.

References:
    - Knuth, D. E. (1998). The Art of Computer Programming, Vol. 3, 5.2.3.
"""

from __future__ import annotations

import random

random.seed(42)

# variant: {variant}


# === SORTING ===


def selection_sort(values: list[int]) -> list[int]:
    """Sort integers by repeatedly selecting the minimum of the unsorted suffix.

    Args:
        values: Integers to sort.

    Returns:
        A new ascending list.

    Complexity:
        O(n^2) time, O(n) space.
    """
    result = list(values)
    # Invariant: result[:start] is sorted and holds the smallest `start` items.
    for start in range(len(result)):
        smallest = start
        for index in range(start + 1, len(result)):
            if result[index] < result[smallest]:
                smallest = index
        result[start], result[smallest] = result[smallest], result[start]
    return result


if __name__ == "__main__":
    assert selection_sort([3, 1, 2]) == [1, 2, 3]
    assert selection_sort([]) == []
    data = [random.randint(0, 100) for _ in range(200)]
    assert selection_sort(data) == sorted(data)
    {final_line}
'''

_INSTRUMENTED = '''"""Selection sort instrumented with step, operation and state trace records."""

from __future__ import annotations

import json

# variant: {variant}


def selection_sort_traced(values: list[int]) -> list[dict[str, object]]:
    """Return one trace record per swap of the selection sort.

    Args:
        values: Integers to sort.

    Returns:
        Trace records with step, operation and state keys.
    """
    result = list(values)
    trace: list[dict[str, object]] = []
    for start in range(len(result)):
        smallest = min(range(start, len(result)), key=result.__getitem__)
        result[start], result[smallest] = result[smallest], result[start]
        trace.append({{"step": start, "operation": "swap", "state": list(result)}})
    return trace


if __name__ == "__main__":
    print(json.dumps(selection_sort_traced([3, 1, 2])))
'''

_SCENE = '''"""Manim scene for selection sort."""

from manim import Scene, Text

# variant: {variant}


class SelectionSortScene(Scene):
    """Shows the selection sort minimum search."""

    def construct(self) -> None:
        self.add(Text("selection sort"))
'''

_CARDS = (
    "front,back,tags,type\n"
    '"What does selection sort select each pass?","The minimum of the unsorted suffix",selection,concept\n'
    '"Selection sort time complexity?","O(n^2)",selection,complexity\n'
    '"How does selection sort place the minimum?","It swaps it to the front",selection,implementation\n'
    '"Selection sort vs insertion sort swaps?","At most n-1 swaps ({variant})",selection,comparison\n'
)


def fixture_outputs(
    variant: str = "A", *, failing_implementation: bool = False, omit: tuple[str, ...] = ()
) -> dict[str, str]:
    """Trusted fixture completions per agent role (offline, deterministic)."""
    final = (
        'assert selection_sort([2, 1]) == [2, 1], "fixture: deliberate failure"'
        if failing_implementation
        else 'print("selection sort: all checks passed")'
    )
    outputs = {
        "drafter": _PASSING_IMPLEMENTATION.format(variant=variant, final_line=final),
        "instrumentation": _INSTRUMENTED.format(variant=variant),
        "visualization": _SCENE.format(variant=variant),
        "assessment": _CARDS.format(variant=variant),
        "reviewer": "passed",
    }
    for role in omit:
        outputs[role] = ""
    return outputs


@dataclass
class OfflineRemotes:
    """Local bare repositories standing in for the supported GitHub repositories.

    `https://github.com/no-magic-ai/<repo>.git` is rewritten to these bare
    repositories through an isolated GIT_CONFIG_GLOBAL, git may only use the
    file transport (GIT_ALLOW_PROTOCOL=file), and `gh` on PATH is a recording
    stand-in, so packaging runs real git without any remote effect.
    """

    bare: dict[str, Path]
    gh_log: Path

    def gh_calls(self) -> list[list[str]]:
        if not self.gh_log.exists():
            return []
        return [json.loads(line) for line in self.gh_log.read_text().splitlines()]

    def branches(self, repository: str) -> list[str]:
        out = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
            cwd=self.bare[repository],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return sorted(out.split())

    def hold_after_receive(self, repository: str, gate: Path) -> None:
        """Keep a push to `repository` open after its refs are updated, until `gate` is released.

        A post-receive hook blocks reading the FIFO `gate`, so the remote branch
        exists while the pushing client still waits for an acknowledgement.
        """
        os.mkfifo(gate)
        hook = self.bare[repository] / "hooks" / "post-receive"
        hook.write_text(f"#!/bin/sh\ncat '{gate}' > /dev/null\n")
        hook.chmod(0o755)

    def hang_pull_requests(self, gate: Path) -> None:
        """Make `gh` record each pull request request, then hang until `gate` is released."""
        os.mkfifo(gate)
        gh = self.gh_log.parent / "bin" / "gh"
        gh.write_text(
            _HANGING_GH.format(python=sys.executable, log=str(self.gh_log), gate=str(gate))
        )

    def reject_pushes(self, repository: str) -> None:
        hook = self.bare[repository] / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'offline fixture: push rejected' >&2\nexit 1\n")
        hook.chmod(0o755)

    def mutate_staged_python(self, repository: str) -> None:
        """Make native git rewrite staged `*.py` content through a clean filter.

        The repository's main branch gains a `.gitattributes` routing `*.py`
        through a filter defined in the isolated global git config, so a
        clone's `git add` stores different bytes than the working file.
        """
        config = Path(os.environ["GIT_CONFIG_GLOBAL"])
        with config.open("a", encoding="utf-8") as handle:
            handle.write(
                '[filter "offline-mutate"]\n\tclean = sed -e s/$/_mutated/\n\trequired = true\n'
            )
        seed = self.bare[repository].parent / f"attributes-{self.bare[repository].stem}"
        subprocess.run(["git", "clone", "-q", str(self.bare[repository]), str(seed)], check=True)
        (seed / ".gitattributes").write_text("*.py filter=offline-mutate\n")
        subprocess.run(["git", "add", ".gitattributes"], cwd=seed, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "route python through a filter"], cwd=seed, check=True
        )
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)

    def blob(self, repository: str, ref: str, path: str) -> bytes:
        return subprocess.run(
            ["git", "cat-file", "blob", f"{ref}:{path}"],
            cwd=self.bare[repository],
            capture_output=True,
            check=True,
        ).stdout


_HANGING_GH = """#!{python}
import json, sys
with open({log!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
with open({gate!r}, encoding="utf-8") as gate:
    gate.read()
"""


def release(gate: Path) -> None:
    """Release whatever is blocked reading the FIFO `gate`; nothing to do if no reader is left."""
    try:
        fd = os.open(gate, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ENXIO:
            return
        raise
    os.close(fd)


@pytest.fixture
def short_publish_deadline(monkeypatch: pytest.MonkeyPatch) -> int:
    """Shorten only the push and `gh pr create` deadlines in this test (product deadlines unchanged)."""
    deadline = 5
    real_run = subprocess.run

    def run(args: list[str], *positional: Any, **keyword: Any) -> Any:
        if args[:2] == ["git", "push"] or args[:3] == ["gh", "pr", "create"]:
            keyword["timeout"] = deadline
        return real_run(args, *positional, **keyword)

    monkeypatch.setattr(subprocess, "run", run)
    return deadline


_FAKE_GH = """#!{python}
import json, os, sys
with open({log!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
with open({log!r}, encoding="utf-8") as handle:
    number = sum(1 for _ in handle)
if os.environ.get("OFFLINE_GH_FAIL_ON") == str(number):
    sys.stderr.write("offline gh stand-in: configured failure\\n")
    sys.exit(1)
repo = sys.argv[sys.argv.index("--repo") + 1]
print(f"https://github.com/{{repo}}/pull/offline-{{number}}")
"""


@pytest.fixture
def offline_remotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OfflineRemotes:
    root = tmp_path / "offline-remotes"
    root.mkdir()
    gitconfig = root / "gitconfig"
    bare: dict[str, Path] = {}
    lines = ["[user]", "\tname = Offline Fixture", "\temail = offline@fixture.invalid"]
    for repository in ("no-magic-ai/no-magic", "no-magic-ai/no-magic-viz"):
        name = repository.split("/")[1]
        bare[repository] = root / f"{name}.git"
        lines += [
            f'[url "{bare[repository].as_uri()}"]',
            f"\tinsteadOf = https://github.com/{repository}.git",
        ]
    gitconfig.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.delenv("OFFLINE_GH_FAIL_ON", raising=False)

    for repository, path in bare.items():
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True)
        seed = root / f"seed-{path.stem}"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "README.md").write_text(f"{repository}\n")
        existing = seed / ("02-alignment" if repository.endswith("/no-magic") else "scenes")
        existing.mkdir()
        (existing / "README.md").write_text("existing\n")
        subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=seed, check=True)
        subprocess.run(["git", "push", "-q", str(path), "main"], cwd=seed, check=True)

    bin_dir = root / "bin"
    bin_dir.mkdir()
    gh_log = root / "gh-calls.jsonl"
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH.format(python=sys.executable, log=str(gh_log)))
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return OfflineRemotes(bare=bare, gh_log=gh_log)
