"""Run-owned artifact files, sealed bundles and their canonical manifests.

Every generated artifact is written under a root owned by exactly one run (or
one legacy pipeline invocation) using a fixed role filename, so two runs of the
same algorithm can never overwrite each other. Completing a run seals its final
roles into an immutable bundle described by a canonical manifest; a human
approval binds that manifest's digest, and every later reader re-verifies the
bundle bytes against it.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apprentice.models.artifact import ArtifactBundle
from apprentice.models.work_item import WorkItem, WorkItemStatus

MANIFEST_VERSION = 1
MANIFEST_FILENAME = "manifest.json"

# Fixed on-disk name for every artifact role. Paths never embed the algorithm
# name or any other caller-provided component.
ROLE_FILENAMES: dict[str, str] = {
    "implementation": "implementation.py",
    "instrumented": "instrumented.py",
    "manim_scene": "scene.py",
    "anki_deck": "cards.csv",
    "validation_report": "validation_report.json",
    "discovery": "discovery.json",
}

# Roles a build run produces, keyed to the ADK session-state output that holds them.
BUNDLE_ROLE_STATE_KEYS: dict[str, str] = {
    "implementation": "generated_code",
    "instrumented": "instrumented_code",
    "manim_scene": "manim_scene_code",
    "anki_deck": "anki_deck_content",
}

CORE_REPOSITORY = "no-magic-ai/no-magic"
VIZ_REPOSITORY = "no-magic-ai/no-magic-viz"

_TIER_DIRS: dict[int, str] = {
    1: "01-foundations",
    2: "02-alignment",
    3: "03-systems",
    4: "04-agents",
}

_ALGORITHM_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_MANIFEST_KEYS = frozenset({"version", "run_id", "algorithm", "tier", "artifacts"})
_ENTRY_KEYS = frozenset({"role", "path", "size", "sha256", "destination"})


class ArtifactError(Exception):
    """Raised when run-owned artifacts are missing, unsafe or inconsistent."""


@dataclass(frozen=True)
class RunScope:
    """Identity and exclusive mutable work root of one generation run."""

    run_id: str
    algorithm: str
    tier: int
    work_root: Path

    def work_item(self) -> WorkItem:
        """Return the gate work item carrying this run's identity."""
        return WorkItem(
            id=self.run_id,
            algorithm_name=self.algorithm,
            tier=self.tier,
            status=WorkItemStatus.IN_PROGRESS,
        )


@dataclass(frozen=True)
class BundleArtifact:
    """One verified artifact of a sealed bundle, with its captured bytes."""

    role: str
    path: str
    size: int
    sha256: str
    destination: dict[str, str] | None
    data: bytes


@dataclass(frozen=True)
class BundleSnapshot:
    """Verified, in-memory copy of a sealed bundle and its manifest identity."""

    run_id: str
    algorithm: str
    tier: int
    manifest_sha256: str
    artifacts: tuple[BundleArtifact, ...]

    def describe(self) -> list[dict[str, Any]]:
        """Return role, path, size, digest and destination for every artifact."""
        return [
            {
                "role": artifact.role,
                "path": artifact.path,
                "size": artifact.size,
                "sha256": artifact.sha256,
                "destination": artifact.destination,
            }
            for artifact in self.artifacts
        ]


def validate_algorithm_name(name: str) -> str:
    """Return `name` if it is a supported algorithm identifier, else raise."""
    if not _ALGORITHM_NAME.fullmatch(name):
        raise ArtifactError(
            f"unsupported algorithm name {name!r}: use 1-64 characters of lowercase "
            "letters, digits and underscores, starting with a letter"
        )
    return name


def tier_directory(tier: int) -> str:
    """Return the no-magic tier directory for `tier`, rejecting unknown tiers."""
    if isinstance(tier, bool) or tier not in _TIER_DIRS:
        raise ArtifactError(f"unsupported tier {tier!r}: expected one of {sorted(_TIER_DIRS)}")
    return _TIER_DIRS[tier]


def promoted_destinations(algorithm: str, tier: int) -> dict[str, dict[str, str]]:
    """Return the repository destination of every role that submit promotes.

    Roles absent from the result (instrumented code, Anki cards) are retained
    in the approved bundle but are not promoted to any repository.
    """
    name = validate_algorithm_name(algorithm)
    return {
        "implementation": {
            "repository": CORE_REPOSITORY,
            "path": f"{tier_directory(tier)}/micro{name}.py",
        },
        "manim_scene": {
            "repository": VIZ_REPOSITORY,
            "path": f"scenes/scene_micro{name}.py",
        },
    }


def require_owned_root(root: Path | str) -> Path:
    """Return `root` as a Path if it is an existing, absolute, non-symlink directory."""
    if not str(root):
        raise ArtifactError("no artifact root provided; allocate one with SessionStore")
    path = Path(root)
    if not path.is_absolute():
        raise ArtifactError(f"artifact root must be absolute: {path}")
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ArtifactError(f"artifact root does not exist: {path}") from None
    if stat.S_ISLNK(info.st_mode):
        raise ArtifactError(f"artifact root is a symlink: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise ArtifactError(f"artifact root is not a directory: {path}")
    return path


def _role_filename(role: str) -> str:
    if role not in ROLE_FILENAMES:
        raise ArtifactError(f"unknown artifact role {role!r}")
    return ROLE_FILENAMES[role]


def _open_single_link_file(path: Path, flags: int, mode: int = 0o644) -> int:
    """Open `path` without following links and require a single-link regular file."""
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ArtifactError(f"refusing to follow symlink at {path}") from exc
        raise ArtifactError(f"cannot open artifact {path}: {exc.strerror}") from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise ArtifactError(f"artifact {path} is not a single-link regular file")
    return fd


def write_role(root: Path | str, role: str, content: str) -> Path:
    """Write `content` as the fixed-name `role` file inside an owned root."""
    path = require_owned_root(root) / _role_filename(role)
    fd = _open_single_link_file(path, os.O_WRONLY | os.O_CREAT)
    with os.fdopen(fd, "wb") as handle:
        handle.truncate(0)
        handle.write(content.encode("utf-8"))
    return path


def _remove_role(root: Path, role: str) -> None:
    path = root / _role_filename(role)
    if os.path.lexists(path):
        path.unlink()


def state_role_contents(state: dict[str, Any]) -> dict[str, str]:
    """Return the non-empty bundle role contents held in ADK session state."""
    contents: dict[str, str] = {}
    for role, key in BUNDLE_ROLE_STATE_KEYS.items():
        value = state.get(key, "")
        if not value:
            continue
        if not isinstance(value, str):
            raise ArtifactError(f"session state {key!r} is {type(value).__name__}, not text")
        contents[role] = value
    return contents


def write_state_roles(root: Path | str, state: dict[str, Any]) -> dict[str, Path]:
    """Mirror the session-state bundle roles into an owned root.

    Roles absent from state are removed so the root never presents a stale
    artifact from an earlier iteration.
    """
    directory = require_owned_root(root)
    contents = state_role_contents(state)
    paths: dict[str, Path] = {}
    for role in BUNDLE_ROLE_STATE_KEYS:
        if role in contents:
            paths[role] = write_role(directory, role, contents[role])
        else:
            _remove_role(directory, role)
    return paths


def artifact_bundle(bundle_id: str, paths: dict[str, Path]) -> ArtifactBundle:
    """Build the gate-facing ArtifactBundle for role paths in an owned root."""
    return ArtifactBundle(
        id=bundle_id,
        work_item_id=bundle_id,
        implementation_path=str(paths.get("implementation", "")),
        instrumented_path=str(paths.get("instrumented", "")),
        manim_scene_path=str(paths.get("manim_scene", "")),
        anki_deck_path=str(paths.get("anki_deck", "")),
    )


def canonical_json(value: Any) -> bytes:
    """Serialize `value` as canonical UTF-8 JSON (sorted keys, fixed separators)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def build_manifest(
    run_id: str, algorithm: str, tier: int, files: dict[str, bytes]
) -> dict[str, Any]:
    """Return the manifest body binding run identity, artifacts and destinations."""
    destinations = promoted_destinations(algorithm, tier)
    artifacts = [
        {
            "role": role,
            "path": _role_filename(role),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "destination": destinations.get(role),
        }
        for role, data in sorted(files.items())
    ]
    return {
        "version": MANIFEST_VERSION,
        "run_id": run_id,
        "algorithm": algorithm,
        "tier": tier,
        "artifacts": artifacts,
    }


def manifest_digest(body: dict[str, Any]) -> str:
    """Return the SHA-256 of a manifest body's canonical JSON (digest field excluded)."""
    return hashlib.sha256(canonical_json(body)).hexdigest()


def _write_new_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def seal_bundle(
    bundle_dir: Path,
    *,
    run_id: str,
    algorithm: str,
    tier: int,
    contents: dict[str, str],
) -> str:
    """Seal role contents into a new immutable bundle and return its manifest digest.

    The bundle is staged in a sibling directory and renamed into place only
    once every file and the manifest are written; an existing bundle is never
    replaced.
    """
    if "implementation" not in contents:
        raise ArtifactError(f"run {run_id} produced no implementation; nothing to seal")
    unknown = sorted(set(contents) - set(BUNDLE_ROLE_STATE_KEYS))
    if unknown:
        raise ArtifactError(f"cannot seal unsupported roles: {unknown}")
    parent = require_owned_root(bundle_dir.parent)
    if os.path.lexists(bundle_dir):
        raise ArtifactError(f"run {run_id} already has a sealed bundle at {bundle_dir}")

    files = {role: text.encode("utf-8") for role, text in contents.items()}
    body = build_manifest(run_id, algorithm, tier, files)
    digest = manifest_digest(body)

    staging = parent / f".bundle-staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o755)
    for role, data in files.items():
        _write_new_file(staging / _role_filename(role), data)
    _write_new_file(
        staging / MANIFEST_FILENAME, canonical_json({**body, "manifest_sha256": digest})
    )

    if os.path.lexists(bundle_dir):
        raise ArtifactError(f"run {run_id} already has a sealed bundle at {bundle_dir}")
    staging.rename(bundle_dir)
    return digest


def _read_regular(path: Path) -> bytes:
    fd = _open_single_link_file(path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _parse_manifest(raw: bytes, source: Path) -> tuple[dict[str, Any], str]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"malformed manifest {source}: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS | {"manifest_sha256"}:
        raise ArtifactError(f"manifest {source} does not have the supported field set")
    if raw != canonical_json(manifest):
        raise ArtifactError(f"manifest {source} is not in canonical form")
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    digest = manifest["manifest_sha256"]
    if type(body["version"]) is not int or body["version"] != MANIFEST_VERSION:
        raise ArtifactError(f"unsupported manifest version {body['version']!r} in {source}")
    if manifest_digest(body) != digest:
        raise ArtifactError(f"manifest {source} digest does not match its content")
    return body, digest


def _validate_entries(body: dict[str, Any], source: Path) -> list[dict[str, Any]]:
    algorithm = body["algorithm"]
    tier = body["tier"]
    if not isinstance(algorithm, str) or not isinstance(body["run_id"], str):
        raise ArtifactError(f"manifest {source} has a malformed identity")
    destinations = promoted_destinations(algorithm, tier)
    entries = body["artifacts"]
    if not isinstance(entries, list) or not entries:
        raise ArtifactError(f"manifest {source} lists no artifacts")
    roles: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            raise ArtifactError(f"manifest {source} has a malformed artifact entry")
        role = entry["role"]
        if role not in BUNDLE_ROLE_STATE_KEYS:
            raise ArtifactError(f"manifest {source} lists unsupported role {role!r}")
        if entry["path"] != ROLE_FILENAMES[role]:
            raise ArtifactError(f"manifest {source} uses an unsupported path for {role}")
        if entry["destination"] != destinations.get(role):
            raise ArtifactError(
                f"manifest {source} destination for {role} differs from the "
                f"{algorithm} tier {tier} destination"
            )
        size = entry["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ArtifactError(f"manifest {source} has an invalid size for {role}")
        if not isinstance(entry["sha256"], str) or not _SHA256_HEX.fullmatch(entry["sha256"]):
            raise ArtifactError(f"manifest {source} has an invalid digest for {role}")
        roles.append(role)
    if roles != sorted(set(roles)):
        raise ArtifactError(f"manifest {source} artifact roles are duplicated or unsorted")
    if "implementation" not in roles:
        raise ArtifactError(f"manifest {source} has no implementation artifact")
    return entries


def load_snapshot(bundle_dir: Path) -> BundleSnapshot:
    """Verify a sealed bundle against its manifest and capture its bytes.

    Rejects a malformed or non-canonical manifest, unsupported roles, paths or
    destinations, symlinked or multiply-linked files, any added or removed
    file, and any size or SHA-256 divergence.
    """
    directory = require_owned_root(bundle_dir)
    manifest_path = directory / MANIFEST_FILENAME
    body, digest = _parse_manifest(_read_regular(manifest_path), manifest_path)
    entries = _validate_entries(body, manifest_path)

    expected_names = {entry["path"] for entry in entries} | {MANIFEST_FILENAME}
    present_names = {entry.name for entry in os.scandir(directory)}
    if present_names != expected_names:
        raise ArtifactError(
            f"bundle {directory} file set differs from its manifest: "
            f"added={sorted(present_names - expected_names)} "
            f"removed={sorted(expected_names - present_names)}"
        )

    artifacts: list[BundleArtifact] = []
    for entry in entries:
        data = _read_regular(directory / entry["path"])
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ArtifactError(f"bundle artifact {entry['role']} bytes differ from its manifest")
        artifacts.append(
            BundleArtifact(
                role=entry["role"],
                path=entry["path"],
                size=entry["size"],
                sha256=entry["sha256"],
                destination=entry["destination"],
                data=data,
            )
        )
    return BundleSnapshot(
        run_id=body["run_id"],
        algorithm=body["algorithm"],
        tier=body["tier"],
        manifest_sha256=digest,
        artifacts=tuple(artifacts),
    )
