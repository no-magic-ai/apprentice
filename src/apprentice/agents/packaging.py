"""Deterministic packaging — promotes an approved bundle's exact bytes into pull requests.

Nothing here resolves a model, runs the generation graph, drafts text or
renders media. The input is a `BundleSnapshot` already verified against the
approval; its captured bytes are written to their manifest destinations in
fresh clones of the fixed supported repositories, committed with only those
paths, checked against the commit tree, then pushed and opened as pull
requests. Every repository is prepared and verified before anything is pushed.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from apprentice.core.artifacts import CORE_REPOSITORY, VIZ_REPOSITORY

if TYPE_CHECKING:
    from apprentice.core.artifacts import BundleSnapshot

# Supported repositories in publication order; the core PR is referenced by the viz PR.
_REPOSITORY_URLS: dict[str, str] = {
    CORE_REPOSITORY: "https://github.com/no-magic-ai/no-magic.git",
    VIZ_REPOSITORY: "https://github.com/no-magic-ai/no-magic-viz.git",
}


class PackagingError(Exception):
    """Raised when packaging cannot promote the approved bytes exactly.

    Attributes:
        effects: Every prepared repository with whether its branch was pushed
            and the pull request opened for it, as known at the failure. Empty
            when the failure happened before any push.
    """

    def __init__(self, message: str, effects: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.effects = effects or []


@dataclass(frozen=True)
class RepositorySubmission:
    """One repository's promoted branch, verified commit, push state and pull request."""

    repository: str
    base: str
    branch: str
    commit: str
    paths: tuple[str, ...]
    pushed: bool = False
    pr_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "base": self.base,
            "branch": self.branch,
            "commit": self.commit,
            "paths": list(self.paths),
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
    except subprocess.TimeoutExpired as exc:
        raise PackagingError(f"{' '.join(args[:3])} timed out after {timeout}s") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise PackagingError(f"{' '.join(args[:3])} failed ({result.returncode}): {stderr}")
    return result.stdout


def _git(clone: Path, *args: str, timeout: int = 30, env: dict[str, str] | None = None) -> bytes:
    return _run(["git", *args], clone, timeout, env)


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


def _prepare(
    repository: str,
    files: list[tuple[PurePosixPath, bytes]],
    snapshot: BundleSnapshot,
    approval: dict[str, Any],
    workspace: Path,
) -> RepositorySubmission:
    clone = workspace / repository.split("/")[1]
    _run(["git", "clone", "--depth", "1", _REPOSITORY_URLS[repository], str(clone)], workspace, 120)
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
    _git(clone, "commit", "--no-verify", "-q", "-m", message, env=commit_env)
    for destination, data in files:
        if _git(clone, "cat-file", "blob", f"HEAD:{destination}") != data:
            raise PackagingError(
                f"{repository} commit content of {destination} differs from approval"
            )
    committed = _git(clone, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "HEAD")
    if sorted(name for name in committed.decode().split("\0") if name) != sorted(paths):
        raise PackagingError(f"{repository} commit touches files outside the approved destinations")
    commit = _git(clone, "rev-parse", "HEAD").decode().strip()
    return RepositorySubmission(repository, base, branch, commit, tuple(paths))


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
    snapshot: BundleSnapshot, approval: dict[str, Any], workspace: Path
) -> list[RepositorySubmission]:
    """Promote a verified approved snapshot into one pull request per repository.

    Args:
        snapshot: Bundle bytes already verified against the approval.
        approval: The run's approval (approver and time are recorded in commits).
        workspace: Fresh exclusive directory for the repository clones.

    Returns:
        One submission per promoted repository, core repository first.

    Raises:
        PackagingError: If any clone, placement, commit check, push or pull
            request fails; `effects` records which branches were pushed and
            which pull requests were opened before the failure.
    """
    prepared = [
        _prepare(repository, files, snapshot, approval, workspace)
        for repository, files in _group_by_repository(snapshot).items()
    ]
    state = list(prepared)
    for index, submission in enumerate(state):
        clone = workspace / submission.repository.split("/")[1]
        try:
            _git(clone, "push", "origin", f"HEAD:refs/heads/{submission.branch}", timeout=120)
        except PackagingError as exc:
            raise PackagingError(str(exc), [item.to_dict() for item in state]) from exc
        state[index] = replace(submission, pushed=True)

    companion = ""
    for index, submission in enumerate(state):
        clone = workspace / submission.repository.split("/")[1]
        try:
            pr_url = (
                _run(
                    [
                        "gh",
                        "pr",
                        "create",
                        "--repo",
                        submission.repository,
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
            raise PackagingError(str(exc), [item.to_dict() for item in state]) from exc
        state[index] = replace(submission, pr_url=pr_url)
        companion = companion or pr_url
    return state
