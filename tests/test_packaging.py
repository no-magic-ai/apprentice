"""Deterministic packaging against offline bare repositories (real git, recording gh)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import apprentice.agents.packaging as packaging
from apprentice.agents.packaging import (
    PackagingError,
    PublicationSteps,
    RepositorySubmission,
    submit_snapshot,
)
from tests.conftest import release

if TYPE_CHECKING:
    from apprentice.core.artifacts import BundleSnapshot
    from apprentice.core.session_store import SessionStore
    from tests.conftest import OfflineRemotes

_CORE = "no-magic-ai/no-magic"
_VIZ = "no-magic-ai/no-magic-viz"
_APPROVAL = {"approved_by": "tester", "approved_at": "2026-10-06T12:00:00+00:00"}
_CORE_PR = "https://github.com/no-magic-ai/no-magic/pull/offline-1"
_VIZ_PR = "https://github.com/no-magic-ai/no-magic-viz/pull/offline-2"


def _admit_all(prepared: list[RepositorySubmission]) -> None:
    """Final admission that refuses nothing (the CLI applies the configured limits)."""


def _submit(
    snapshot: BundleSnapshot, approval: dict[str, str], workspace: Path
) -> list[RepositorySubmission]:
    return submit_snapshot(
        snapshot, approval, workspace, PublicationSteps(admit=_admit_all, before_step=_any_step)
    )


def _any_step(step: str) -> None:
    """Per-step liveness belongs to the caller (the CLI checks the ledger); none here."""


def _snapshot(store: SessionStore, algorithm: str = "selection") -> BundleSnapshot:
    record = store.create_run(algorithm, 2)
    store.complete_run(
        record,
        {
            "generated_code": "impl = 1\n",
            "manim_scene_code": "scene = 1\n",
            "instrumented_code": "trace = 1\n",
            "anki_deck_content": "front,back\n",
        },
        {},
        1.0,
    )
    return store.load_bundle(record)


def test_promotes_exact_approved_bytes_to_each_destination(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    snapshot = _snapshot(store)

    submissions = _submit(snapshot, _APPROVAL, store.allocate_work_root())

    branch = f"apprentice/{snapshot.run_id}"
    assert [s.repository for s in submissions] == [_CORE, _VIZ]
    by_role = {a.role: a.data for a in snapshot.artifacts}
    assert (
        offline_remotes.blob(_CORE, branch, "02-alignment/microselection.py")
        == by_role["implementation"]
    )
    assert (
        offline_remotes.blob(_VIZ, branch, "scenes/scene_microselection.py")
        == by_role["manim_scene"]
    )
    assert submissions[0].paths == ("02-alignment/microselection.py",)
    assert submissions[1].paths == ("scenes/scene_microselection.py",)


def test_commit_touches_only_approved_paths(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    snapshot = _snapshot(store)
    submissions = _submit(snapshot, _APPROVAL, store.allocate_work_root())

    changed = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", submissions[0].commit],
        cwd=offline_remotes.bare[_CORE],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert changed == ["02-alignment/microselection.py"]


def test_same_approval_produces_the_same_commit(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    snapshot = _snapshot(store)
    first = _submit(snapshot, _APPROVAL, store.allocate_work_root())

    second = _submit(snapshot, _APPROVAL, store.allocate_work_root())

    assert [s.commit for s in second] == [s.commit for s in first]


def test_viz_pull_request_references_core_pull_request(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    snapshot = _snapshot(store)
    submissions = _submit(snapshot, _APPROVAL, store.allocate_work_root())

    calls = offline_remotes.gh_calls()
    assert [call[call.index("--repo") + 1] for call in calls] == [
        f"github.com/{_CORE}",
        f"github.com/{_VIZ}",
    ]
    viz_body = calls[1][calls[1].index("--body") + 1]
    assert f"Companion PR: {submissions[0].pr_url}" in viz_body
    assert snapshot.manifest_sha256 in viz_body


def test_existing_destination_fails_before_any_push(
    store: SessionStore, offline_remotes: OfflineRemotes, tmp_path: Path
) -> None:
    seed = tmp_path / "collide"
    subprocess.run(["git", "clone", "-q", str(offline_remotes.bare[_VIZ]), str(seed)], check=True)
    (seed / "scenes" / "scene_microselection.py").write_text("upstream = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "upstream"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)

    with pytest.raises(PackagingError, match="already exists"):
        _submit(_snapshot(store), _APPROVAL, store.allocate_work_root())

    assert offline_remotes.branches(_CORE) == ["main"]
    assert offline_remotes.branches(_VIZ) == ["main"]
    assert offline_remotes.gh_calls() == []


def test_symlinked_destination_parent_fails_before_any_push(
    store: SessionStore, offline_remotes: OfflineRemotes, tmp_path: Path
) -> None:
    seed = tmp_path / "link"
    subprocess.run(["git", "clone", "-q", str(offline_remotes.bare[_CORE]), str(seed)], check=True)
    subprocess.run(["git", "rm", "-q", "-r", "02-alignment"], cwd=seed, check=True)
    os.symlink("/tmp", seed / "02-alignment")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "link"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)

    with pytest.raises(PackagingError, match="not a plain directory"):
        _submit(_snapshot(store), _APPROVAL, store.allocate_work_root())

    assert offline_remotes.branches(_CORE) == ["main"]
    assert offline_remotes.gh_calls() == []


def _effects(
    excinfo: pytest.ExceptionInfo[PackagingError],
) -> list[tuple[str, bool | None, str | None]]:
    return [(e["repository"], e["pushed"], e["pr_url"]) for e in excinfo.value.effects]


def test_failure_before_any_push_reports_no_effects(
    store: SessionStore, offline_remotes: OfflineRemotes, tmp_path: Path
) -> None:
    seed = tmp_path / "collide-core"
    subprocess.run(["git", "clone", "-q", str(offline_remotes.bare[_CORE]), str(seed)], check=True)
    (seed / "02-alignment" / "microselection.py").write_text("upstream = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "upstream"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)

    with pytest.raises(PackagingError) as excinfo:
        _submit(_snapshot(store), _APPROVAL, store.allocate_work_root())

    assert excinfo.value.effects == []


def test_second_push_failure_reports_the_pushed_core_branch(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    offline_remotes.reject_pushes(_VIZ)
    snapshot = _snapshot(store)

    with pytest.raises(PackagingError, match="push rejected") as excinfo:
        _submit(snapshot, _APPROVAL, store.allocate_work_root())

    assert _effects(excinfo) == [(_CORE, True, ""), (_VIZ, False, "")]
    assert f"apprentice/{snapshot.run_id}" in offline_remotes.branches(_CORE)
    assert offline_remotes.gh_calls() == []


def test_second_pull_request_failure_reports_the_opened_core_pr(
    store: SessionStore, offline_remotes: OfflineRemotes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OFFLINE_GH_FAIL_ON", "2")

    with pytest.raises(PackagingError, match="configured failure") as excinfo:
        _submit(_snapshot(store), _APPROVAL, store.allocate_work_root())

    assert _effects(excinfo) == [(_CORE, True, _CORE_PR), (_VIZ, True, "")]


def test_packaging_uses_captured_bytes_even_if_the_bundle_changes_after_verification(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    snapshot = _snapshot(store)
    approved = {a.role: a.data for a in snapshot.artifacts}
    bundle = store.bundle_dir(snapshot.run_id)
    for name in ("implementation.py", "scene.py"):
        path = bundle / name
        path.chmod(0o644)
        path.write_bytes(b"swapped_after_verification = True\n")

    _submit(snapshot, _APPROVAL, store.allocate_work_root())

    branch = f"apprentice/{snapshot.run_id}"
    assert (
        offline_remotes.blob(_CORE, branch, "02-alignment/microselection.py")
        == approved["implementation"]
    )
    assert (
        offline_remotes.blob(_VIZ, branch, "scenes/scene_microselection.py")
        == approved["manim_scene"]
    )


def test_commit_whose_stored_blob_differs_is_never_pushed(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    offline_remotes.mutate_staged_python(_CORE)

    with pytest.raises(PackagingError, match="differs from approval") as excinfo:
        _submit(_snapshot(store), _APPROVAL, store.allocate_work_root())

    assert excinfo.value.effects == []
    assert offline_remotes.branches(_CORE) == ["main"]
    assert offline_remotes.branches(_VIZ) == ["main"]
    assert offline_remotes.gh_calls() == []


def test_extra_path_staged_by_native_git_is_never_committed_or_pushed(
    store: SessionStore, offline_remotes: OfflineRemotes, tmp_path: Path
) -> None:
    # A post-checkout hook in the isolated global config stages an extra file
    # in every clone, so `git add` of the approved paths leaves more staged.
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text("#!/bin/sh\necho extra > EXTRA.txt\ngit add EXTRA.txt\n")
    hook.chmod(0o755)
    with open(os.environ["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8") as config:
        config.write(f"[core]\n\thooksPath = {hooks}\n")

    with pytest.raises(PackagingError) as excinfo:
        _submit(_snapshot(store), _APPROVAL, store.allocate_work_root())

    assert excinfo.value.effects == []
    assert offline_remotes.branches(_CORE) == ["main"]
    assert offline_remotes.branches(_VIZ) == ["main"]
    assert offline_remotes.gh_calls() == []


def test_extra_path_added_by_a_post_commit_amend_is_never_pushed(
    store: SessionStore, offline_remotes: OfflineRemotes, tmp_path: Path
) -> None:
    # A post-commit hook amends every commit once to add an extra file, after
    # the staged-path check has passed; only the committed-path check sees it.
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "post-commit"
    hook.write_text(
        "#!/bin/sh\n"
        'if [ -z "$FIXTURE_AMENDED" ]; then\n'
        "  export FIXTURE_AMENDED=1\n"
        "  echo extra > EXTRA.txt\n"
        "  git add EXTRA.txt\n"
        "  git commit -q --amend --no-edit --no-verify\n"
        "fi\n"
    )
    hook.chmod(0o755)
    with open(os.environ["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8") as config:
        config.write(f"[core]\n\thooksPath = {hooks}\n")

    with pytest.raises(PackagingError) as excinfo:
        _submit(_snapshot(store), _APPROVAL, store.allocate_work_root())

    assert excinfo.value.effects == []
    assert offline_remotes.branches(_CORE) == ["main"]
    assert offline_remotes.branches(_VIZ) == ["main"]
    assert offline_remotes.gh_calls() == []


def _commit_message(offline_remotes: OfflineRemotes, repository: str, branch: str) -> bytes:
    commit = subprocess.run(
        ["git", "cat-file", "commit", branch],
        cwd=offline_remotes.bare[repository],
        capture_output=True,
        check=True,
    ).stdout
    return commit.split(b"\n\n", 1)[1]


@pytest.mark.parametrize("approver", ["tester", "  padded ", "tab\t", "Zo\u00eb  ", " \tboth\t "])
def test_commit_message_keeps_the_exact_approver_bytes(
    store: SessionStore, offline_remotes: OfflineRemotes, approver: str
) -> None:
    snapshot = _snapshot(store)

    _submit(snapshot, {**_APPROVAL, "approved_by": approver}, store.allocate_work_root())

    expected = (
        f"Add microselection\n\n"
        f"Apprentice-Run: {snapshot.run_id}\n"
        f"Apprentice-Manifest: {snapshot.manifest_sha256}\n"
        f"Approved-By: {approver}\n"
    ).encode()
    branch = f"apprentice/{snapshot.run_id}"
    assert _commit_message(offline_remotes, _CORE, branch) == expected
    assert _commit_message(offline_remotes, _VIZ, branch) == expected


def _only_on_path(tmp_path: Path, name: str, executable: Path | None) -> Path:
    """A directory holding just `name`: a link to `executable`, or a non-executable file."""
    directory = tmp_path / f"only-{name}"
    directory.mkdir()
    if executable is None:
        (directory / name).write_text("#!/bin/sh\nexit 0\n")
        (directory / name).chmod(0o644)
    else:
        (directory / name).symlink_to(executable)
    return directory


def test_gh_that_cannot_be_started_after_both_pushes_reports_both_pushed_branches(
    store: SessionStore,
    offline_remotes: OfflineRemotes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = shutil.which("git")
    assert git is not None
    snapshot = _snapshot(store)
    gh_dir = _only_on_path(tmp_path, "gh", None)
    git_dir = _only_on_path(tmp_path, "git", Path(git))
    monkeypatch.setenv("PATH", f"{gh_dir}{os.pathsep}{git_dir}")

    with pytest.raises(PackagingError) as excinfo:
        _submit(snapshot, _APPROVAL, store.allocate_work_root())

    assert _effects(excinfo) == [(_CORE, True, ""), (_VIZ, True, "")]
    branch = f"apprentice/{snapshot.run_id}"
    assert branch in offline_remotes.branches(_CORE)
    assert branch in offline_remotes.branches(_VIZ)
    assert offline_remotes.gh_calls() == []


@pytest.mark.parametrize("git_present", [False, True], ids=["git-missing", "git-not-executable"])
def test_git_that_cannot_be_found_or_started_fails_before_any_effect(
    store: SessionStore,
    offline_remotes: OfflineRemotes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_present: bool,
) -> None:
    snapshot = _snapshot(store)
    path = _only_on_path(tmp_path, "git", None) if git_present else tmp_path / "empty"
    path.mkdir(exist_ok=True)
    workspace = store.allocate_work_root()

    with monkeypatch.context() as only_this_git, pytest.raises(PackagingError) as excinfo:
        only_this_git.setenv("PATH", str(path))
        _submit(snapshot, _APPROVAL, workspace)

    assert excinfo.value.effects == []
    assert offline_remotes.branches(_CORE) == ["main"]
    assert offline_remotes.branches(_VIZ) == ["main"]
    assert offline_remotes.gh_calls() == []


def test_shortened_publish_deadline_alone_lets_packaging_complete(
    store: SessionStore, offline_remotes: OfflineRemotes, short_publish_deadline: int
) -> None:
    submissions = _submit(_snapshot(store), _APPROVAL, store.allocate_work_root())

    assert [(s.pushed, s.pr_url) for s in submissions] == [(True, _CORE_PR), (True, _VIZ_PR)]


@pytest.mark.parametrize("repository", [_CORE, _VIZ])
def test_push_that_times_out_after_the_remote_updated_is_unknown_not_unpushed(
    store: SessionStore,
    offline_remotes: OfflineRemotes,
    tmp_path: Path,
    short_publish_deadline: int,
    repository: str,
) -> None:
    gate = tmp_path / "receive-gate"
    offline_remotes.hold_after_receive(repository, gate)
    snapshot = _snapshot(store)
    try:
        with pytest.raises(PackagingError) as excinfo:
            _submit(snapshot, _APPROVAL, store.allocate_work_root())
    finally:
        release(gate)

    branch = f"apprentice/{snapshot.run_id}"
    if repository == _CORE:
        assert _effects(excinfo) == [(_CORE, None, ""), (_VIZ, False, "")]
        assert offline_remotes.branches(_VIZ) == ["main"]
    else:
        assert _effects(excinfo) == [(_CORE, True, ""), (_VIZ, None, "")]
    assert branch in offline_remotes.branches(repository)
    assert offline_remotes.gh_calls() == []


def test_pull_request_that_times_out_after_gh_ran_is_unknown_not_absent(
    store: SessionStore,
    offline_remotes: OfflineRemotes,
    tmp_path: Path,
    short_publish_deadline: int,
) -> None:
    gate = tmp_path / "gh-gate"
    offline_remotes.hang_pull_requests(gate)
    try:
        with pytest.raises(PackagingError) as excinfo:
            _submit(_snapshot(store), _APPROVAL, store.allocate_work_root())
    finally:
        release(gate)

    assert _effects(excinfo) == [(_CORE, True, None), (_VIZ, True, "")]
    (call,) = offline_remotes.gh_calls()
    assert call[:2] == ["pr", "create"] and call[call.index("--repo") + 1] == f"github.com/{_CORE}"


def test_each_prepared_commit_is_measured_before_admission(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    seen: list[list[dict[str, object]]] = []

    def record(prepared: list[RepositorySubmission]) -> None:
        seen.append([submission.size.to_dict() for submission in prepared])
        assert offline_remotes.gh_calls() == []

    submit_snapshot(
        _snapshot(store),
        _APPROVAL,
        store.allocate_work_root(),
        PublicationSteps(admit=record, before_step=_any_step),
    )

    assert seen == [
        [
            {"files": 1, "text_lines": 1, "binary": []},
            {"files": 1, "text_lines": 1, "binary": []},
        ]
    ]


def test_refused_admission_pushes_nothing(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    def refuse(prepared: list[RepositorySubmission]) -> None:
        raise RuntimeError("admission refused")

    with pytest.raises(RuntimeError, match="admission refused"):
        submit_snapshot(
            _snapshot(store),
            _APPROVAL,
            store.allocate_work_root(),
            PublicationSteps(admit=refuse, before_step=_any_step),
        )

    assert offline_remotes.branches(_CORE) == ["main"]
    assert offline_remotes.branches(_VIZ) == ["main"]
    assert offline_remotes.gh_calls() == []


def _amend(clone: Path) -> None:
    target = next(p for p in clone.rglob("*micro*.py") if ".git" not in p.parts)
    target.write_bytes(target.read_bytes() + b"# not approved\n")
    subprocess.run(["git", "commit", "-q", "-a", "--amend", "--no-edit"], cwd=clone, check=True)


@pytest.mark.parametrize(
    ("tampered_before", "repository", "published"),
    [
        ("push no-magic-ai/no-magic-viz", "no-magic-viz", {_CORE: True, _VIZ: False}),
        ("open pull request in no-magic-ai/no-magic", "no-magic", {_CORE: True, _VIZ: True}),
    ],
)
def test_prepared_commit_changed_between_remote_steps_stops_the_next_effect(
    store: SessionStore,
    offline_remotes: OfflineRemotes,
    tampered_before: str,
    repository: str,
    published: dict[str, bool],
) -> None:
    workspace = store.allocate_work_root()

    def tamper(step: str) -> None:
        if step == tampered_before:
            _amend(workspace / repository)

    steps = PublicationSteps(admit=_admit_all, before_step=tamper)
    with pytest.raises(PackagingError, match="was replaced by"):
        submit_snapshot(_snapshot(store), _APPROVAL, workspace, steps)

    assert offline_remotes.gh_calls() == []
    assert {s.repository: s.pushed for s in steps.progress} == published
    for name, pushed in published.items():
        assert ("apprentice/" in " ".join(offline_remotes.branches(name))) == pushed


def test_refused_step_check_stops_before_the_next_remote_effect(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    def lost(step: str) -> None:
        if step == "push no-magic-ai/no-magic-viz":
            raise RuntimeError("own cycle no longer live")

    steps = PublicationSteps(admit=_admit_all, before_step=lost)
    with pytest.raises(RuntimeError):
        submit_snapshot(_snapshot(store), _APPROVAL, store.allocate_work_root(), steps)

    assert [s.pushed for s in steps.progress] == [True, False]
    assert offline_remotes.branches(_VIZ) == ["main"]
    assert offline_remotes.gh_calls() == []


def _git_config(*lines: str) -> None:
    with open(os.environ["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8") as config:
        config.write("\n".join(lines) + "\n")


def _rewrite(offline_remotes: OfflineRemotes, kind: str) -> None:
    """Route the core repository's supported URL to the viz repository through git config."""
    core = packaging._REPOSITORY_URLS[_CORE]
    viz = offline_remotes.bare[_VIZ].as_uri()
    _git_config(f'[url "{viz}"]', f"\t{kind} = {core}")


@pytest.mark.parametrize("kind", ["insteadOf", "pushInsteadOf"])
def test_git_routing_the_core_url_elsewhere_is_refused_before_any_commit_or_push(
    store: SessionStore, offline_remotes: OfflineRemotes, kind: str
) -> None:
    _rewrite(offline_remotes, kind)
    workspace = store.allocate_work_root()

    with pytest.raises(PackagingError) as excinfo:
        _submit(_snapshot(store), _APPROVAL, workspace)

    assert excinfo.value.effects == []
    assert offline_remotes.branches(_CORE) == ["main"]
    assert offline_remotes.branches(_VIZ) == ["main"]
    assert offline_remotes.gh_calls() == []
    if kind == "insteadOf":
        assert not (workspace / "no-magic").exists()


def test_destination_rerouted_after_the_first_push_stops_every_later_step(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    snapshot = _snapshot(store)
    workspace = store.allocate_work_root()
    viz_clone = workspace / "no-magic-viz"

    def before_step(step: str) -> None:
        if step == f"push {_VIZ}":
            subprocess.run(
                ["git", "remote", "set-url", "--add", "--push", "origin", str(viz_clone)],
                cwd=viz_clone,
                check=True,
            )

    with pytest.raises(PackagingError) as excinfo:
        submit_snapshot(
            snapshot,
            _APPROVAL,
            workspace,
            PublicationSteps(admit=_admit_all, before_step=before_step),
        )

    branch = f"apprentice/{snapshot.run_id}"
    assert [(e["repository"], e["pushed"]) for e in excinfo.value.effects] == [
        (_CORE, True),
        (_VIZ, False),
    ]
    assert offline_remotes.branches(_CORE) == [branch, "main"]
    assert offline_remotes.branches(_VIZ) == ["main"]
    assert offline_remotes.gh_calls() == []


def test_supported_destinations_publish_to_their_own_repositories(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    snapshot = _snapshot(store)

    submissions = _submit(snapshot, _APPROVAL, store.allocate_work_root())

    branch = f"apprentice/{snapshot.run_id}"
    assert [s.repository for s in submissions] == [_CORE, _VIZ]
    assert offline_remotes.branches(_CORE) == [branch, "main"]
    assert offline_remotes.branches(_VIZ) == [branch, "main"]
    assert offline_remotes.blob(_CORE, branch, "02-alignment/microselection.py") == b"impl = 1\n"
    assert offline_remotes.blob(_VIZ, branch, "scenes/scene_microselection.py") == b"scene = 1\n"


@pytest.mark.parametrize("hostile", ["GH_HOST", "GH_REPO"])
def test_pull_requests_are_opened_on_the_supported_host_whatever_gh_is_told(
    store: SessionStore,
    offline_remotes: OfflineRemotes,
    monkeypatch: pytest.MonkeyPatch,
    hostile: str,
) -> None:
    monkeypatch.setenv(hostile, "ghe.example.invalid" if hostile == "GH_HOST" else "other/repo")
    snapshot = _snapshot(store)

    submissions = _submit(snapshot, _APPROVAL, store.allocate_work_root())

    calls = offline_remotes.gh_calls()
    assert [call[call.index("--repo") + 1] for call in calls] == [
        f"github.com/{_CORE}",
        f"github.com/{_VIZ}",
    ]
    assert [s.pr_url for s in submissions] == [_CORE_PR, _VIZ_PR]
