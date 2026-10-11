"""Deterministic packaging — promotes an approved bundle's exact bytes into pull requests.

Nothing here resolves a model, runs the generation graph, drafts text or
renders media. The input is a `BundleSnapshot` already verified against the
approval; its captured bytes are written to their manifest destinations in
fresh clones of the fixed supported repositories, committed with only those
paths, checked against the commit tree, then pushed and opened as pull
requests. Every repository is prepared, verified and measured before anything
is pushed; the caller's final admission sees every measured change and runs
immediately before the first push.

No subprocess inherits the process environment or the operator's git or gh
configuration. Before anything is prepared, the operator's commit identity
(name and email only) is read once from their git configuration; every git
command of a publication — clone, checkout, add, commit, the local checks and
reads, the push — then runs with a private empty HOME, no global or system
git configuration, that explicit identity, no credential helper, hooks,
fsmonitor or commit signing, no HTTP redirects, no proxy, host or trace
settings and only the destination URL's protocol. Nothing but the push and
`gh pr create` receives the caller's explicit `PublisherCapability`.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from apprentice.core.artifacts import CORE_REPOSITORY, VIZ_REPOSITORY

if TYPE_CHECKING:
    from collections.abc import Callable

    from apprentice.controls.credentials import PublisherCapability
    from apprentice.core.artifacts import BundleSnapshot

# Supported repositories in publication order; the core PR is referenced by the viz PR.
# This map is the only authority for where each repository's approved bytes go:
# git must resolve both fetch and push of a clone to exactly these URLs.
_GITHUB_HOST = "github.com"
_REPOSITORY_URLS: dict[str, str] = {
    CORE_REPOSITORY: f"https://{_GITHUB_HOST}/{CORE_REPOSITORY}.git",
    VIZ_REPOSITORY: f"https://{_GITHUB_HOST}/{VIZ_REPOSITORY}.git",
}

# What every publisher subprocess keeps from the process environment.
_INHERITED_VARIABLES = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
# Where the operator's own git configuration (and so their identity) is found.
_CONFIG_VARIABLES = (
    "HOME",
    "XDG_CONFIG_HOME",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
)


def _inherited(names: tuple[str, ...]) -> dict[str, str]:
    return {name: os.environ[name] for name in names if name in os.environ}


def _operator_identity(workspace: Path) -> dict[str, str]:
    """The operator's commit author and committer, as git would choose them, as env variables.

    Read once, before anything is prepared, with a read-only `git config`
    query outside any repository; it runs no helper or hook and only these
    four scalars cross into the isolated publisher environment.

    Raises:
        PackagingError: If git cannot be queried or no complete identity is configured.
    """
    env = {
        **_inherited(_INHERITED_VARIABLES + _CONFIG_VARIABLES),
        "GIT_CEILING_DIRECTORIES": str(workspace.parent),
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        result = subprocess.run(
            ["git", "config", "--null", "--get-regexp", r"^(user|author|committer)\.(name|email)$"],
            cwd=workspace,
            capture_output=True,
            timeout=30,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PackagingError(f"cannot read the operator's git identity: {exc}") from exc
    if result.returncode not in (0, 1):  # 1: none of the keys is set
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise PackagingError(f"cannot read the operator's git identity: {stderr}")
    configured: dict[str, str] = {}
    for entry in result.stdout.decode("utf-8", "replace").split("\0"):
        key, _, value = entry.partition("\n")
        if key:
            configured[key] = value  # later (more specific) configuration wins

    def choose(variable: str, *keys: str) -> str:
        candidates = [os.environ.get(variable, ""), *(configured.get(key, "") for key in keys)]
        return next((value for value in candidates if value), "")

    email = os.environ.get("EMAIL", "")
    identity = {
        "GIT_AUTHOR_NAME": choose("GIT_AUTHOR_NAME", "author.name", "user.name"),
        "GIT_AUTHOR_EMAIL": choose("GIT_AUTHOR_EMAIL", "author.email", "user.email") or email,
        "GIT_COMMITTER_NAME": choose("GIT_COMMITTER_NAME", "committer.name", "user.name"),
        "GIT_COMMITTER_EMAIL": choose("GIT_COMMITTER_EMAIL", "committer.email", "user.email")
        or email,
    }
    missing = sorted(name for name, value in identity.items() if not value)
    if missing:
        raise PackagingError(
            f"no git commit identity for {', '.join(missing)}: set user.name and user.email "
            "in your git configuration; nothing was cloned or pushed"
        )
    return identity


def _isolated(home: Path) -> dict[str, str]:
    return {
        **_inherited(_INHERITED_VARIABLES),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git_env(
    home: Path,
    url: str,
    identity: dict[str, str],
    capability: PublisherCapability | None = None,
) -> dict[str, str]:
    """The environment of every git command of the repository at `url`.

    Only `url`'s protocol is allowed; credential helpers, hooks, fsmonitor
    and commit signing are off, so no external program runs; HTTP redirects
    are refused (a redirect would send the approved bytes to another host or
    repository); the commit identity is `identity`. The HTTP authorization
    header for the supported host is set only when `capability` is given
    (the push).
    """
    settings = [
        ("credential.helper", ""),
        ("core.hooksPath", os.devnull),
        ("core.fsmonitor", "false"),
        ("commit.gpgSign", "false"),
        ("http.followRedirects", "false"),
    ]
    if capability is not None:
        settings.append((f"http.https://{_GITHUB_HOST}/.extraHeader", capability.git_http_header()))
    env = {**_isolated(home), **identity, "GIT_ALLOW_PROTOCOL": urlsplit(url).scheme}
    env["GIT_CONFIG_COUNT"] = str(len(settings))
    for index, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _gh_env(home: Path, capability: PublisherCapability) -> dict[str, str]:
    """The environment of `gh pr create`: the explicit token only, no gh or git configuration."""
    return {
        **_isolated(home),
        "GH_CONFIG_DIR": str(home / "gh"),
        "GH_TOKEN": capability.gh_token(),
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "NO_COLOR": "1",
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


def _run(
    args: list[str],
    cwd: Path,
    timeout: int,
    env: dict[str, str],
    capability: PublisherCapability | None = None,
) -> bytes:
    """Run one fixed command with exactly `env`; the capability is redacted from any error."""
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
        if capability is not None:
            stderr = capability.redact(stderr)
        raise PackagingError(f"{' '.join(args[:3])} failed ({result.returncode}): {stderr}")
    return result.stdout


def _git(
    clone: Path,
    env: dict[str, str],
    *args: str,
    timeout: int = 30,
    capability: PublisherCapability | None = None,
) -> bytes:
    """Run git in `clone` with exactly `env` (from `_git_env`)."""
    return _run(["git", *args], clone, timeout, env, capability)


def _require_destination(repository: str, clone: Path, env: dict[str, str]) -> None:
    """The clone fetches from and pushes to exactly the supported URL, checked without the network.

    `origin` must have exactly that one fetch URL and that one push URL, after
    any rewrite or `pushurl` in the clone's own configuration (the operator's
    global and system configuration never apply). This binds the approved
    destination to the logical repository; it is not a check of the remote
    host's identity or of the account.

    Raises:
        PackagingError: If git would fetch from or push anywhere else.
    """
    expected = _REPOSITORY_URLS[repository]
    resolved = _git(clone, env, "remote", "get-url", "--all", "origin").decode().split()
    pushes = _git(clone, env, "remote", "get-url", "--push", "--all", "origin").decode().split()
    if resolved != [expected] or pushes != [expected]:
        raise PackagingError(
            f"{repository} resolves to fetch {resolved} / push {pushes} instead of its supported "
            f"URL {expected} (a URL rewrite or extra push URL is configured in the clone); "
            "nothing more is committed or pushed there"
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
    env: dict[str, str],
) -> RepositorySubmission:
    clone = workspace / repository.split("/")[1]
    # The public clone is read without any credential.
    _run(
        ["git", "clone", "--depth", "1", _REPOSITORY_URLS[repository], str(clone)],
        workspace,
        120,
        env,
    )
    _require_destination(repository, clone, env)
    base = _git(clone, env, "symbolic-ref", "--short", "HEAD").decode().strip()
    branch = f"apprentice/{snapshot.run_id}"
    _git(clone, env, "checkout", "-b", branch)

    paths = [str(destination) for destination, _ in files]
    for destination, data in files:
        _place(clone, destination, data)
    _git(clone, env, "add", "--", *paths)
    staged = _git(clone, env, "diff", "--cached", "--name-only", "-z").decode().split("\0")
    if sorted(name for name in staged if name) != sorted(paths):
        raise PackagingError(f"{repository} staged files {staged} differ from {paths}")

    # Author and committer dates come from the approval, so the same approved
    # bundle on the same base always produces the same commit.
    commit_env = {
        **env,
        "GIT_AUTHOR_DATE": approval["approved_at"],
        "GIT_COMMITTER_DATE": approval["approved_at"],
    }
    message = (
        f"Add micro{snapshot.algorithm}\n\n"
        f"Apprentice-Run: {snapshot.run_id}\n"
        f"Apprentice-Manifest: {snapshot.manifest_sha256}\n"
        f"Approved-By: {approval['approved_by']}\n"
    )
    _git(clone, commit_env, "commit", "--no-verify", "--cleanup=verbatim", "-q", "-m", message)
    commit = _git(clone, env, "rev-parse", "HEAD").decode().strip()
    _verify_commit(clone, env, repository, commit, files)
    return RepositorySubmission(
        repository, base, branch, commit, tuple(paths), measure_commit(clone, env)
    )


def _verify_commit(
    clone: Path,
    env: dict[str, str],
    repository: str,
    commit: str,
    files: list[tuple[PurePosixPath, bytes]],
) -> None:
    """Require that `commit` changes exactly the approved destinations to the approved bytes.

    Raises:
        PackagingError: On any differing byte, extra or missing path.
    """
    for destination, data in files:
        if _git(clone, env, "cat-file", "blob", f"{commit}:{destination}") != data:
            raise PackagingError(
                f"{repository} commit content of {destination} differs from approval"
            )
    committed = _git(clone, env, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit)
    paths = sorted(str(destination) for destination, _ in files)
    if sorted(name for name in committed.decode().split("\0") if name) != paths:
        raise PackagingError(f"{repository} commit touches files outside the approved destinations")


def _require_prepared(
    clone: Path,
    env: dict[str, str],
    submission: RepositorySubmission,
    files: list[tuple[PurePosixPath, bytes]],
) -> None:
    """Re-verify a prepared repository immediately before one of its remote steps.

    The clone must still fetch from and push to exactly the repository's
    supported URL, its HEAD must still be the prepared commit, and that
    commit must still hold exactly the approved bytes and paths.

    Raises:
        PackagingError: If the destination, the prepared commit or its content changed.
    """
    _require_destination(submission.repository, clone, env)
    head = _git(clone, env, "rev-parse", "HEAD").decode().strip()
    if head != submission.commit:
        raise PackagingError(
            f"{submission.repository} prepared commit {submission.commit} was replaced by {head}"
        )
    _verify_commit(clone, env, submission.repository, submission.commit, files)


def measure_commit(clone: Path, env: dict[str, str]) -> ChangeSize:
    """Count the prepared commit's changed paths and text lines from git's numstat.

    Raises:
        PackagingError: If any numstat record cannot be classified.
    """
    raw = _git(clone, env, "diff", "--numstat", "-z", "-M", "HEAD~1", "HEAD").decode(
        "utf-8", "strict"
    )
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
            ref = "HEAD~1" if not _exists(clone, env, "HEAD", paths[-1]) else "HEAD"
            size = _git(clone, env, "cat-file", "-s", f"{ref}:{paths[-1]}").decode().strip()
            if not size.isdigit():
                raise PackagingError(f"unknown size of binary file {paths[-1]}")
            binary.append((paths[-1], int(size)))
        elif added.isdigit() and deleted.isdigit():
            lines += int(added) + int(deleted)
        else:
            raise PackagingError(f"unclassifiable numstat counts {added!r}/{deleted!r} for {paths}")
    return ChangeSize(files=files, text_lines=lines, binary=tuple(binary))


def _exists(clone: Path, env: dict[str, str], ref: str, path: str) -> bool:
    try:
        _git(clone, env, "cat-file", "-e", f"{ref}:{path}")
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
    capability: PublisherCapability,
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
        capability: The explicit publication credential, already validated
            before anything was admitted; only the push and `gh pr create`
            subprocesses receive it, after `steps.admit`.

    Returns:
        One submission per promoted repository, core repository first.

    Raises:
        PackagingError: If any clone, placement, commit check, re-verification,
            push or pull request fails; `effects` records which branches were
            pushed and which pull requests were opened before the failure,
            with None for a step whose outcome is unknown.
    """
    groups = _group_by_repository(snapshot)
    identity = _operator_identity(workspace)
    # A private, empty HOME for every publisher subprocess: no gh login, git
    # configuration, hook or credential store of the operator is consulted.
    home = workspace / "publisher-home"
    home.mkdir(mode=0o700)
    envs = {
        repository: _git_env(home, _REPOSITORY_URLS[repository], identity) for repository in groups
    }
    prepared = [
        _prepare(repository, files, snapshot, approval, workspace, envs[repository])
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
            _require_prepared(
                clone, envs[submission.repository], submission, groups[submission.repository]
            )
        except PackagingError as exc:
            raise failed(exc) from exc
        state[index] = replace(submission, pushed=None)
        try:
            _git(
                clone,
                _git_env(home, _REPOSITORY_URLS[submission.repository], identity, capability),
                "push",
                "origin",
                f"{submission.commit}:refs/heads/{submission.branch}",
                timeout=120,
                capability=capability,
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
            env = envs[submission.repository]
            _require_prepared(clone, env, submission, groups[submission.repository])
            remote = _git(clone, env, "ls-remote", "origin", f"refs/heads/{submission.branch}")
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
                        # supported host whatever gh's configuration says;
                        # the only credential is the explicit capability.
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
                    _gh_env(home, capability),
                    capability,
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
