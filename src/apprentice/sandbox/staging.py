"""Trusted staging: bounded bytes of regular files inside a caller-authorized root, never links.

The root is the trust anchor and is always supplied by the trusted caller (a
run's work root allocated by the session store); a path from a model or an
artifact's own parent directory is only ever resolved inside it. Every
component below the root is opened relative to its parent without following
symlinks, and the file must be a single-link regular file within the size
limit. A path that names anything else — outside the root, through a link, a
directory, FIFO, socket, device, hard-linked or oversized file — is refused
with `UnsafeInputError` before anything is read.

Producer precondition: the trusted caller's producer has finished writing the
file (closed, or atomically published) before it is staged, as apprentice's
own single-owner writers do. A process of the same host user that keeps
writing in place is outside this boundary: the identity, size and time checks
around the read detect such a change only on a best-effort basis (an in-place
write or mmap store need not change them), and are not a coherence guarantee.
Once captured, the bounded bytes are private and immutable: only they are
staged into the sandbox, never the host file.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from apprentice.core.artifacts import ArtifactError, require_owned_root
from apprentice.sandbox.errors import OutsideScopeError, StagingError, UnsafeInputError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

# Upper bound of any generated source or text artifact staged for validation.
MAX_ARTIFACT_BYTES = 1 << 20

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
# Errors with which opening a path refuses its kind (a link, or a socket or device).
_UNSAFE_OPEN = {errno.ELOOP, errno.ENXIO, errno.EOPNOTSUPP, errno.ENODEV}


def _open_root(root: Path) -> int:
    try:
        directory = require_owned_root(root)
    except ArtifactError as exc:
        raise StagingError(str(exc)) from exc
    try:
        return os.open(directory, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise StagingError(f"cannot open artifact root {directory}: {exc.strerror}") from exc


def scope_label(root: Path) -> str:
    """Return a stable label naming the trusted root `root` (its device and inode)."""
    fd = _open_root(root)
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    return hashlib.sha256(f"{info.st_dev}:{info.st_ino}".encode()).hexdigest()[:32]


def relative_parts(root: Path, path: str | os.PathLike[str]) -> tuple[str, ...]:
    """Return the components of `path` below `root`, refusing anything that leaves it.

    `path` is either relative to `root` or absolute and lexically below it.

    Raises:
        OutsideScopeError: If `path` is empty, outside `root` or uses `.`/`..`.
    """
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(root)
        except ValueError:
            raise OutsideScopeError(f"{path} is outside the artifact root {root}") from None
    parts = candidate.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise OutsideScopeError(f"{path!r} does not name a file inside the artifact root {root}")
    return parts


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_bounded(fd: int, path: object, limit: int) -> bytes:
    """Read the regular file open at `fd` (which the caller closes), refusing other kinds."""
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise UnsafeInputError(f"{path} is not a single-link regular file")
    if before.st_size > limit:
        raise UnsafeInputError(f"{path} has {before.st_size} bytes; at most {limit} are staged")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(1 << 16, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            break
    after = os.fstat(fd)
    if _identity(before) != _identity(after) or total != before.st_size:
        # Best-effort: an in-place writer need not change any of these.
        raise StagingError(f"{path} changed while it was staged")
    return b"".join(chunks)


def read_scoped(root: Path, path: str | os.PathLike[str], *, limit: int) -> bytes | None:
    """Return the bytes of the regular file `path` inside `root`, or None if it is absent.

    Every descriptor opened here is closed on every path, success or failure.
    The file's producer must have finished writing it (see the module notes).

    Raises:
        UnsafeInputError: If `path` leaves `root` (`OutsideScopeError`), a
            component is a link or not a directory, or the file is a link, not a
            single-link regular file, or exceeds `limit` bytes.
        StagingError: If the root is unusable, the file cannot be read, or a
            change while it was read was detected.
    """
    parts = relative_parts(root, path)
    descriptors = [_open_root(root)]
    try:
        for part in parts[:-1]:
            try:
                descriptors.append(os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptors[-1]))
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise UnsafeInputError(
                        f"{path}: {part!r} is a link or not a directory"
                    ) from exc
                raise StagingError(f"{path}: cannot open {part!r}: {exc.strerror}") from exc
        try:
            descriptors.append(os.open(parts[-1], _FILE_FLAGS, dir_fd=descriptors[-1]))
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in _UNSAFE_OPEN:
                raise UnsafeInputError(f"{path} is a link or a special file") from exc
            raise StagingError(f"cannot open {path}: {exc.strerror}") from exc
        try:
            return _read_bounded(descriptors[-1], path, limit)
        except OSError as exc:
            raise StagingError(f"cannot read {path}: {exc.strerror}") from exc
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


@contextmanager
def staged_copies(root: Path, paths: Mapping[str, str]) -> Iterator[dict[str, str]]:
    """Copy each path's bytes from `root` into a private directory for the block.

    Each value of the result names the private copy (same file name, its own
    subdirectory), or a missing path when the source is absent, so readers
    never open anything in `root` themselves. Empty paths stay empty.

    Raises:
        UnsafeInputError: As `read_scoped`.
        StagingError: As `read_scoped`, or if a private copy cannot be written.
    """
    with tempfile.TemporaryDirectory(prefix="apprentice-stage-") as private:
        staged: dict[str, str] = {}
        for index, (key, path) in enumerate(paths.items()):
            if not path:
                staged[key] = ""
                continue
            name = relative_parts(root, path)[-1]
            target = Path(private) / str(index) / name
            data = read_scoped(root, path, limit=MAX_ARTIFACT_BYTES)
            if data is not None:
                try:
                    target.parent.mkdir(mode=0o700)
                    fd = os.open(
                        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                    )
                    try:
                        _write_all(fd, data)
                    finally:
                        os.close(fd)
                except OSError as exc:
                    raise StagingError(f"cannot stage a private copy of {path}") from exc
            staged[key] = str(target)
        yield staged


def write_derived(root: Path, directory: str, name: str, data: bytes) -> Path:
    """Atomically write verified derived bytes as `root/directory/name` without following links.

    Raises:
        StagingError: If the root or `directory` is unusable or a link, or the
            bytes cannot be written and placed (no partial file is left and no
            descriptor stays open).
    """
    root_fd = _open_root(root)
    try:
        with suppress(FileExistsError):
            os.mkdir(directory, 0o755, dir_fd=root_fd)
        try:
            directory_fd = os.open(directory, _DIRECTORY_FLAGS, dir_fd=root_fd)
        except OSError as exc:
            raise StagingError(f"derived output directory {directory!r} is unusable") from exc
        partial = f".{name}.{uuid.uuid4().hex}.partial"
        try:
            fd = os.open(
                partial,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=directory_fd,
            )
            try:
                try:
                    _write_all(fd, data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(partial, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            except BaseException:
                with suppress(FileNotFoundError):
                    os.unlink(partial, dir_fd=directory_fd)
                raise
        except OSError as exc:
            raise StagingError(f"cannot place derived output {name!r}: {exc.strerror}") from exc
        finally:
            os.close(directory_fd)
    finally:
        os.close(root_fd)
    return Path(root) / directory / name
