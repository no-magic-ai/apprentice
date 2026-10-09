"""Deterministic packaging — promotes an approved bundle's exact bytes into pull requests.

Nothing here resolves a model, runs the generation graph, drafts text or
renders media. The input is a `BundleSnapshot` already verified against the
approval; its captured bytes are written to their manifest destinations in
fresh clones of the fixed supported repositories, committed with only those
paths, checked against the commit tree, then pushed and opened as pull
requests. Every repository is prepared, verified and measured before anything
is pushed; the caller's final admission sees every measured change and runs
immediately before the first push.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from apprentice.core.artifacts import CORE_REPOSITORY, VIZ_REPOSITORY

if TYPE_CHECKING:
    from collections.abc import Callable

    from apprentice.core.artifacts import BundleSnapshot

# Supported repositories in publication order; the core PR is referenced by the viz PR.
# This map is the only authority for where each repository's approved bytes go:
# git must resolve both fetch and push of a clone to exactly these URLs.
_GITHUB_HOST = "github.com"
_REPOSITORY_URLS: dict[str, str] = {
    CORE_REPOSITORY: f"https://{_GITHUB_HOST}/{CORE_REPOSITORY}.git",
    VIZ_REPOSITORY: f"https://{_GITHUB_HOST}/{VIZ_REPOSITORY}.git",
}


class PackagingError(Exception):
    """Raised when packaging cannot promote the approved bytes exactly.

    Attributes:
        effects: Every prepared repository with whether its branch was pushed
            and the pull request opened for it, as known at the failure. Empty
            when the failure happened before any push. `pushed` or `pr_url` is
            None when the push or pull request command timed out: it may have
            taken effect without acknowledging it.
    """

    def __init__(self, message: str, effects: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.effects = effects or []


@dataclass
class PublicationSteps:
    """What the caller decides around the remote steps of one submission.

    `admit` runs once over every prepared, measured commit before the first
    push; `before_step` runs before every push and pull request (it raises to
    stop the next effect); `progress` is filled with the live effects.
    """

    admit: Callable[[list[RepositorySubmission]], None]
    before_step: Callable[[str], None]
    progress: list[RepositorySubmission] = field(default_factory=list)


@dataclass(frozen=True)
class ChangeSize:
    """The exact change of one prepared commit against its base.

    `files` counts every changed path (a rename counts its old and new path).
    `text_lines` is added plus deleted lines of text files only; binary files
    have no line count — they are listed with their byte size instead.
    """

    files: int
    text_lines: int
    binary: tuple[tuple[str, int], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "text_lines": self.text_lines,
            "binary": [{"path": path, "bytes": size} for path, size in self.binary],
        }


@dataclass(frozen=True)
class RepositorySubmission:
    """One repository's promoted branch, verified commit, push state and pull request.

    `pushed` and `pr_url` are None only when the push or `gh pr create` timed
    out, so whether the branch or pull request exists is unknown.
    """

    repository: str
    base: str
    branch: str
    commit: str
    paths: tuple[str, ...]
    size: ChangeSize = field(default_factory=lambda: ChangeSize(0, 0))
    pushed: bool | None = False
    pr_url: str | None = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "base": self.base,
            "branch": self.branch,
            "commit": self.commit,
            "paths": list(self.paths),
            "size": self.size.to_dict(),
            "pushed": self.pushed,
            "pr_url": self.pr_url,
        }


def _run(args: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None) -> bytes:
    try:
        result = subprocess.run(
            args, cwd=cwd, capture_output=True, timeout=timeout, check=False, env=env
        )
    except FileNotFoundError as exc:
        raise PackagingError(f"{args[0]} is not installed") from exc
    except OSError as exc:
        raise PackagingError(f"{args[0]} could not be started: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PackagingError(f"{' '.join(args[:3])} timed out after {timeout}s") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise PackagingError(f"{' '.join(args[:3])} failed ({result.returncode}): {stderr}")
    return result.stdout


def _git(clone: Path, *args: str, timeout: int = 30, env: dict[str, str] | None = None) -> bytes:
    return _run(["git", *args], clone, timeout, env)


def _remote_env() -> dict[str, str]:
    """The environment of a git command that talks to a remote: HTTP redirects are refused.

    A redirect would send the approved bytes to a host or repository other
    than the resolved destination; the operator's other git settings
    (credentials, transport) are kept.
    """
    env = dict(os.environ)
    count = env.get("GIT_CONFIG_COUNT", "0")
    if not count.isdigit():
        raise PackagingError(f"GIT_CONFIG_COUNT {count!r} is not a count")
    env[f"GIT_CONFIG_KEY_{count}"] = "http.followRedirects"
    env[f"GIT_CONFIG_VALUE_{count}"] = "false"
    env["GIT_CONFIG_COUNT"] = str(int(count) + 1)
    return env


def _require_destination(repository: str, cwd: Path, clone: Path | None = None) -> None:
    """Git resolves `repository` to exactly its supported URL, checked without the network.

    Before the clone, the URL git would fetch from (after any
    `url.<base>.insteadOf` rewrite) must be the supported URL. In a clone,
    `origin` must have exactly that one fetch URL and that one push URL
    (after `insteadOf`/`pushInsteadOf` rewrites and any `pushurl`). This
    binds the approved destination to the logical repository; it is not a
    check of the remote host's identity or of the account.

    Raises:
        PackagingError: If git would fetch from or push anywhere else.
    """
    expected = _REPOSITORY_URLS[repository]
    if clone is None:
        resolved = [_run(["git", "ls-remote", "--get-url", expected], cwd, 30).decode().strip()]
        pushes = resolved
    else:
        resolved = _git(clone, "remote", "get-url", "--all", "origin").decode().split()
        pushes = _git(clone, "remote", "get-url", "--push", "--all", "origin").decode().split()
    if resolved != [expected] or pushes != [expected]:
        raise PackagingError(
            f"{repository} resolves to fetch {resolved} / push {pushes} instead of its supported "
            f"URL {expected} (a git URL rewrite or extra push URL is configured); nothing is "
            "cloned, committed or pushed there"
        )


def _safe_destination(path: str) -> PurePosixPath:
    relative = PurePosixPath(path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in ("", ".", "..", ".git") for part in relative.parts)
    ):
        raise PackagingError(f"unsupported destination path {path!r}")
    return relative


def _place(clone: Path, destination: PurePosixPath, data: bytes) -> None:
    """Create `destination` inside `clone` without following or replacing anything."""
    directory = clone
    for part in destination.parts[:-1]:
        directory = directory / part
        if os.path.lexists(directory):
            if directory.is_symlink() or not directory.is_dir():
                raise PackagingError(f"destination parent {directory} is not a plain directory")
        else:
            directory.mkdir()
    target = directory / destination.parts[-1]
    if os.path.lexists(target):
        raise PackagingError(f"destination {destination} already exists in the repository")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _group_by_repository(snapshot: BundleSnapshot) -> dict[str, list[tuple[PurePosixPath, bytes]]]:
    groups: dict[str, list[tuple[PurePosixPath, bytes]]] = {}
    for artifact in snapshot.artifacts:
        if artifact.destination is None:
            continue
        repository = artifact.destination["repository"]
        if repository not in _REPOSITORY_URLS:
            raise PackagingError(f"unsupported destination repository {repository!r}")
        groups.setdefault(repository, []).append(
            (_safe_destination(artifact.destination["path"]), artifact.data)
        )
    if CORE_REPOSITORY not in groups:
        raise PackagingError("approved bundle promotes nothing to the core repository")
    return {repo: groups[repo] for repo in _REPOSITORY_URLS if repo in groups}


def intended_repositories(snapshot: BundleSnapshot) -> tuple[str, ...]:
    """Return the repositories the approved bundle promotes into (one PR each), in order.

    Raises:
        PackagingError: If a destination repository or path is unsupported.
    """
    return tuple(_group_by_repository(snapshot))


def _prepare(
    repository: str,
    files: list[tuple[PurePosixPath, bytes]],
    snapshot: BundleSnapshot,
    approval: dict[str, Any],
    workspace: Path,
) -> RepositorySubmission:
    clone = workspace / repository.split("/")[1]
    _require_destination(repository, workspace)
    _run(
        ["git", "clone", "--depth", "1", _REPOSITORY_URLS[repository], str(clone)],
        workspace,
        120,
        _remote_env(),
    )
    _require_destination(repository, workspace, clone)
    base = _git(clone, "symbolic-ref", "--short", "HEAD").decode().strip()
    branch = f"apprentice/{snapshot.run_id}"
    _git(clone, "checkout", "-b", branch)

    paths = [str(destination) for destination, _ in files]
    for destination, data in files:
        _place(clone, destination, data)
    _git(clone, "add", "--", *paths)
    staged = _git(clone, "diff", "--cached", "--name-only", "-z").decode().split("\0")
    if sorted(name for name in staged if name) != sorted(paths):
        raise PackagingError(f"{repository} staged files {staged} differ from {paths}")

    # Author and committer dates come from the approval, so the same approved
    # bundle on the same base always produces the same commit.
    commit_env = {
        **os.environ,
        "GIT_AUTHOR_DATE": approval["approved_at"],
        "GIT_COMMITTER_DATE": approval["approved_at"],
    }
    message = (
        f"Add micro{snapshot.algorithm}\n\n"
        f"Apprentice-Run: {snapshot.run_id}\n"
        f"Apprentice-Manifest: {snapshot.manifest_sha256}\n"
        f"Approved-By: {approval['approved_by']}\n"
    )
    _git(clone, "commit", "--no-verify", "--cleanup=verbatim", "-q", "-m", message, env=commit_env)
    commit = _git(clone, "rev-parse", "HEAD").decode().strip()
    _verify_commit(clone, repository, commit, files)
    return RepositorySubmission(
        repository, base, branch, commit, tuple(paths), measure_commit(clone)
    )


def _verify_commit(
    clone: Path, repository: str, commit: str, files: list[tuple[PurePosixPath, bytes]]
) -> None:
    """Require that `commit` changes exactly the approved destinations to the approved bytes.

    Raises:
        PackagingError: On any differing byte, extra or missing path.
    """
    for destination, data in files:
        if _git(clone, "cat-file", "blob", f"{commit}:{destination}") != data:
            raise PackagingError(
                f"{repository} commit content of {destination} differs from approval"
            )
    committed = _git(clone, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit)
    paths = sorted(str(destination) for destination, _ in files)
    if sorted(name for name in committed.decode().split("\0") if name) != paths:
        raise PackagingError(f"{repository} commit touches files outside the approved destinations")


def _require_prepared(
    clone: Path, submission: RepositorySubmission, files: list[tuple[PurePosixPath, bytes]]
) -> None:
    """Re-verify a prepared repository immediately before one of its remote steps.

    The clone must still fetch from and push to exactly the repository's
    supported URL, its HEAD must still be the prepared commit, and that
    commit must still hold exactly the approved bytes and paths.

    Raises:
        PackagingError: If the destination, the prepared commit or its content changed.
    """
    _require_destination(submission.repository, clone, clone)
    head = _git(clone, "rev-parse", "HEAD").decode().strip()
    if head != submission.commit:
        raise PackagingError(
            f"{submission.repository} prepared commit {submission.commit} was replaced by {head}"
        )
    _verify_commit(clone, submission.repository, submission.commit, files)


def measure_commit(clone: Path) -> ChangeSize:
    """Count the prepared commit's changed paths and text lines from git's numstat.

    Raises:
        PackagingError: If any numstat record cannot be classified.
    """
    raw = _git(clone, "diff", "--numstat", "-z", "-M", "HEAD~1", "HEAD").decode("utf-8", "strict")
    fields = raw.split("\0")
    files = 0
    lines = 0
    binary: list[tuple[str, int]] = []
    index = 0
    while index < len(fields) and fields[index]:
        stat = fields[index].split("\t")
        if len(stat) != 3:
            raise PackagingError(f"unclassifiable numstat record {fields[index]!r}")
        added, deleted, path = stat
        if path:
            paths = [path]
            index += 1
        else:
            paths = fields[index + 1 : index + 3]
            if len(paths) != 2 or not all(paths):
                raise PackagingError(f"unclassifiable rename record {fields[index : index + 3]!r}")
            index += 3
        files += len(paths)
        if (added, deleted) == ("-", "-"):
            ref = "HEAD~1" if not _exists(clone, "HEAD", paths[-1]) else "HEAD"
            size = _git(clone, "cat-file", "-s", f"{ref}:{paths[-1]}").decode().strip()
            if not size.isdigit():
                raise PackagingError(f"unknown size of binary file {paths[-1]}")
            binary.append((paths[-1], int(size)))
        elif added.isdigit() and deleted.isdigit():
            lines += int(added) + int(deleted)
        else:
            raise PackagingError(f"unclassifiable numstat counts {added!r}/{deleted!r} for {paths}")
    return ChangeSize(files=files, text_lines=lines, binary=tuple(binary))


def _exists(clone: Path, ref: str, path: str) -> bool:
    try:
        _git(clone, "cat-file", "-e", f"{ref}:{path}")
    except PackagingError:
        return False
    return True


def _pr_body(snapshot: BundleSnapshot, approval: dict[str, Any], companion: str) -> str:
    lines = [
        f"Promotes the approved apprentice bundle for `{snapshot.algorithm}` "
        f"(tier {snapshot.tier}) byte-for-byte; nothing was regenerated.",
        "",
        f"- Run: `{snapshot.run_id}`",
        f"- Manifest SHA-256: `{snapshot.manifest_sha256}`",
        f"- Approved by `{approval['approved_by']}` at {approval['approved_at']}",
    ]
    for artifact in snapshot.artifacts:
        if artifact.destination is not None:
            lines.append(
                f"- `{artifact.destination['repository']}:{artifact.destination['path']}` "
                f"sha256 `{artifact.sha256}`"
            )
    if companion:
        lines.append(f"- Companion PR: {companion}")
    return "\n".join(lines)


def submit_snapshot(
    snapshot: BundleSnapshot,
    approval: dict[str, Any],
    workspace: Path,
    steps: PublicationSteps,
) -> list[RepositorySubmission]:
    """Promote a verified approved snapshot into one pull request per repository.

    Every repository is prepared, verified and measured, then `steps.admit`
    runs once before the first push. Before every later remote step (each
    push and each pull request) `steps.before_step` runs and the repository's
    prepared commit is re-verified against the approved bytes; the push sends
    that exact commit, never whatever HEAD has become. `steps.progress` holds
    every repository's known effects as they happen: a step that was started
    but not acknowledged is recorded as unknown (None), so an interruption at
    any point leaves an honest account.

    Args:
        snapshot: Bundle bytes already verified against the approval.
        approval: The run's approval (approver and time are recorded in commits).
        workspace: Fresh exclusive directory for the repository clones.
        steps: The caller's admission and per-step checks and the live effects.

    Returns:
        One submission per promoted repository, core repository first.

    Raises:
        PackagingError: If any clone, placement, commit check, re-verification,
            push or pull request fails; `effects` records which branches were
            pushed and which pull requests were opened before the failure,
            with None for a step whose outcome is unknown.
    """
    groups = _group_by_repository(snapshot)
    prepared = [
        _prepare(repository, files, snapshot, approval, workspace)
        for repository, files in groups.items()
    ]
    steps.admit(prepared)
    state = steps.progress
    state[:] = prepared

    def failed(exc: PackagingError) -> PackagingError:
        return PackagingError(str(exc), [item.to_dict() for item in state])

    for index, submission in enumerate(prepared):
        clone = workspace / submission.repository.split("/")[1]
        steps.before_step(f"push {submission.repository}")
        try:
            _require_prepared(clone, submission, groups[submission.repository])
        except PackagingError as exc:
            raise failed(exc) from exc
        state[index] = replace(submission, pushed=None)
        try:
            _git(
                clone,
                "push",
                "origin",
                f"{submission.commit}:refs/heads/{submission.branch}",
                timeout=120,
                env=_remote_env(),
            )
        except PackagingError as exc:
            if not isinstance(exc.__cause__, subprocess.TimeoutExpired):
                state[index] = replace(submission, pushed=False)
            raise failed(exc) from exc
        state[index] = replace(submission, pushed=True)

    companion = ""
    for index, submission in enumerate(list(state)):
        clone = workspace / submission.repository.split("/")[1]
        steps.before_step(f"open pull request in {submission.repository}")
        try:
            _require_prepared(clone, submission, groups[submission.repository])
            remote = _git(
                clone, "ls-remote", "origin", f"refs/heads/{submission.branch}", env=_remote_env()
            )
        except PackagingError as exc:
            raise failed(exc) from exc
        if remote.decode().split("\t")[0].strip() != submission.commit:
            raise failed(
                PackagingError(
                    f"{submission.repository} branch {submission.branch} no longer points at "
                    f"the approved commit {submission.commit}"
                )
            )
        state[index] = replace(submission, pr_url=None)
        try:
            pr_url = (
                _run(
                    [
                        "gh",
                        "pr",
                        "create",
                        # HOST/OWNER/REPO: the pull request is opened on the
                        # supported host whatever GH_HOST or gh's default
                        # host says; credentials are gh's own.
                        "--repo",
                        f"{_GITHUB_HOST}/{submission.repository}",
                        "--base",
                        submission.base,
                        "--head",
                        submission.branch,
                        "--title",
                        f"Add micro{snapshot.algorithm}",
                        "--body",
                        _pr_body(snapshot, approval, companion),
                    ],
                    clone,
                    60,
                )
                .decode()
                .strip()
            )
        except PackagingError as exc:
            if not isinstance(exc.__cause__, subprocess.TimeoutExpired):
                state[index] = replace(submission, pr_url="")
            raise failed(exc) from exc
        state[index] = replace(submission, pr_url=pr_url)
        companion = companion or pr_url
    return list(state)
