"""Fixed trusted programs and commands run inside sandbox containers.

Only these run as the keeper user (`KEEPER_USER`): the hard-lifetime keeper,
the stdin stager and the byte collector. They use the standard library only,
never import or interpret generated files and never parse media: the
collector copies bounded bytes of one fixed path. Generated code, the
renderer and the media probe run as `SOURCE_USER`, which cannot signal, trace
or modify keeper processes or the keeper-owned input files. Each of them is
started through the trusted `launched` wrapper, whose start marker is the
only evidence that Docker started the command: everything after it, exit
status included, belongs to the untrusted program.
"""

from __future__ import annotations

KEEPER_USER = "65531:65531"
SOURCE_USER = "65532:65532"

INPUT_DIR = "/input"
OUTPUT_DIR = "/output"
SCRATCH_DIR = "/tmp"

# Executables of the verified sandbox image that source-user commands run.
PYTHON = "/usr/local/bin/python"
FFPROBE = "/usr/bin/ffprobe"

# Written by the launcher before it replaces itself with the source command.
START_MARKER = b"\x00apprentice-sandbox-started\x00"

# Exit status of the collector when the rendered output itself is unacceptable
# (missing, a link, unreadable, too large or changing): a render failure, not
# an infrastructure fault.
COLLECTOR_REJECTED = 3

# Fixed render geometry; the renderer writes `<media_dir>/videos/scene/180p5/preview.mp4`.
SCENE_FILE = "scene.py"
VIDEO_FILE = "preview.mp4"
RENDER_WIDTH = 320
RENDER_HEIGHT = 180
RENDER_FPS = 5
RENDERED_VIDEO_PARTS = ("videos", "scene", f"{RENDER_HEIGHT}p{RENDER_FPS}", VIDEO_FILE)


def keeper(lifetime_seconds: int) -> list[str]:
    """The container's main process: it ends the whole container after `lifetime_seconds`."""
    return ["python", "-I", "-c", f"import time; time.sleep({int(lifetime_seconds)})"]


_STAGER = """
import os, re, struct, sys
stream = sys.stdin.buffer
def exact(size):
    data = stream.read(size)
    if len(data) != size:
        raise SystemExit("short staging input")
    return data
(count,) = struct.unpack(">I", exact(4))
root = os.open("/input", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
for _ in range(count):
    (length,) = struct.unpack(">H", exact(2))
    name = exact(length).decode("ascii")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}[.](py|mp4)", name):
        raise SystemExit("unsupported staged name")
    (size,) = struct.unpack(">Q", exact(8))
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444, dir_fd=root)
    remaining = size
    while remaining:
        view = memoryview(exact(min(remaining, 65536)))
        remaining -= len(view)
        while view:
            view = view[os.write(fd, view):]
    os.close(fd)
if stream.read(1):
    raise SystemExit("trailing staging input")
sys.stdout.write("staged %d\\n" % count)
"""


def stager() -> list[str]:
    """Write length-framed files from stdin into the keeper-owned, read-only-to-source input."""
    return ["python", "-I", "-c", _STAGER]


_LAUNCHER = """
import os, sys
target = sys.argv[1]
if not os.access(target, os.X_OK):
    sys.stderr.write("sandbox launcher: %s is not executable\\n" % target)
    sys.exit(2)
os.write(1, START_MARKER)
os.dup2(1, 2)
os.execv(target, sys.argv[1:])
""".replace("START_MARKER", repr(START_MARKER))


def launched(command: list[str]) -> list[str]:
    """Start `command` (absolute executable first) after writing `START_MARKER`.

    The launcher is fixed trusted code run as the source user. It writes the
    marker before anything of `command` can run and joins stderr to stdout, so
    the command's output is one ordered stream that always follows the
    marker. Output without the marker came from Docker failing to start the
    launcher, never from the program.
    """
    return ["python", "-I", "-c", _LAUNCHER, *command]


def program_command() -> list[str]:
    """Run the staged generated implementation."""
    return launched([PYTHON, "-I", f"{INPUT_DIR}/implementation.py"])


_COLLECTOR = """
import hashlib, os, stat, struct, sys
LIMIT = {limit}
PARTS = {parts!r}
out = sys.stdout.buffer
def reject(reason):
    out.write(b"reject: " + reason.encode("ascii", "replace"))
    out.flush()
    sys.exit({rejected})
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
try:
    fd = os.open("/output", directory_flags)
except PermissionError:
    reject("render output directory is not readable")
for part in PARTS[:-1]:
    try:
        child = os.open(part, directory_flags, dir_fd=fd)
    except FileNotFoundError:
        reject("no rendered video (missing %s)" % part)
    except PermissionError:
        reject("render output is not readable")
    except OSError:
        reject("render output %s is a link or not a directory" % part)
    os.close(fd)
    fd = child
try:
    video = os.open(PARTS[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
except FileNotFoundError:
    reject("no rendered video")
except PermissionError:
    reject("rendered video is not readable")
except OSError:
    reject("rendered video is a link or special file")
before = os.fstat(video)
if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
    reject("rendered video is not a single-link regular file")
if before.st_size > LIMIT:
    reject("rendered video has %d bytes; at most %d are collected" % (before.st_size, LIMIT))
chunks = []
total = 0
while True:
    chunk = os.read(video, min(65536, LIMIT + 1 - total))
    if not chunk:
        break
    chunks.append(chunk)
    total += len(chunk)
    if total > LIMIT:
        reject("rendered video grew past %d bytes" % LIMIT)
after = os.fstat(video)
def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
if identity(before) != identity(after) or total != before.st_size:
    reject("rendered video changed while it was collected")
data = b"".join(chunks)
out.write(struct.pack(">Q", len(data)))
out.write(data)
out.write(hashlib.sha256(data).digest())
out.flush()
"""


def collector(limit: int) -> list[str]:
    """Stream the fixed rendered video as `size | bytes | sha256`, or reject it.

    The path is traversed from `/output` by directory descriptors without
    following links; the file must be one bounded, single-link regular file
    whose identity and times are unchanged across the read.
    """
    script = _COLLECTOR.format(
        limit=int(limit), parts=RENDERED_VIDEO_PARTS, rejected=COLLECTOR_REJECTED
    )
    return ["python", "-I", "-c", script]


def render_command(scene_class: str) -> list[str]:
    """Render `scene_class` of the staged scene at the fixed preview geometry."""
    return launched(
        [
            PYTHON,
            "-I",
            "-m",
            "manim",
            "-ql",
            "--renderer",
            "cairo",
            "--disable_caching",
            "--progress_bar",
            "none",
            "--fps",
            str(RENDER_FPS),
            "-r",
            f"{RENDER_WIDTH},{RENDER_HEIGHT}",
            "--media_dir",
            OUTPUT_DIR,
            "-o",
            VIDEO_FILE.removesuffix(".mp4"),
            f"{INPUT_DIR}/{SCENE_FILE}",
            scene_class,
        ]
    )


def media_probe_command() -> list[str]:
    """Probe the staged, immutable copy of the collected video (run as the source user)."""
    return launched(
        [
            FFPROBE,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,nb_read_frames",
            "-show_entries",
            "format=duration,size",
            "-of",
            "json",
            f"{INPUT_DIR}/{VIDEO_FILE}",
        ]
    )
