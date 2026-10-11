"""Scene render gate — imports, constructs and renders the generated Manim scene in the sandbox.

Static checks (a `Scene` subclass in the source) cannot show that a scene
imports, constructs or renders. This gate renders the one scene class of the
visualization artifact at a small fixed preview geometry inside the sandbox,
collects the video while the render container is alive, probes the collected
bytes in a fresh container and only then writes them, as derived output
separate from the sealed bundle, to `render/scene.mp4` in the run's root.
A run without a scene artifact has nothing to render.
"""

from __future__ import annotations

import ast
import hashlib
from typing import TYPE_CHECKING, Any

from apprentice.models.work_item import GateResult, GateVerdict, WorkItem
from apprentice.sandbox.staging import MAX_ARTIFACT_BYTES, read_scoped, scope_label, write_derived

if TYPE_CHECKING:
    from pathlib import Path

    from apprentice.models.artifact import ArtifactBundle
    from apprentice.sandbox.docker import Sandbox

RENDER_DIRECTORY = "render"
RENDER_FILENAME = "scene.mp4"
_OUTPUT_EXCERPT_LEN = 600


def scene_class(source: bytes) -> str:
    """Return the one top-level class deriving from a `...Scene` base, read statically.

    Raises:
        ValueError: If the source does not parse or has no or several scene classes.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise ValueError(f"the scene does not parse: {exc}") from exc
    names = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            (isinstance(base, ast.Name) and base.id.endswith("Scene"))
            or (isinstance(base, ast.Attribute) and base.attr.endswith("Scene"))
            for base in node.bases
        )
    ]
    if len(names) != 1:
        raise ValueError(
            f"the scene must define exactly one top-level Scene subclass, found {names or 'none'}"
        )
    return names[0]


class SceneRenderGate:
    """Render the visualization artifact in the sandbox; FAIL unless a verified video results."""

    name = "scene_render"
    max_retries = 0
    blocking = True

    def __init__(self, sandbox: Sandbox, root: Path) -> None:
        self._sandbox = sandbox
        self._root = root

    def evaluate(self, work_item: WorkItem, artifacts: ArtifactBundle) -> GateResult:
        """Render the scene and verify the preview video.

        Raises:
            UnsafeInputError: If the scene path is outside the root or names
                no stageable file.
            SandboxInfrastructureError: If the backend could not render,
                collect, verify or tear down safely.
        """
        path = artifacts.manim_scene_path
        if not path:
            return self._result(
                GateVerdict.PASS, {"rendered": False, "reason": "no scene artifact"}
            )
        source = read_scoped(self._root, path, limit=MAX_ARTIFACT_BYTES)
        if source is None:
            return self._result(GateVerdict.FAIL, {"error": f"file not found: {path}"})
        try:
            name = scene_class(source)
        except ValueError as exc:
            return self._result(GateVerdict.FAIL, {"error": str(exc)})
        render = self._sandbox.render_scene(source, name, scope=scope_label(self._root))
        diagnostics: dict[str, Any] = {
            "scene_class": name,
            "image": self._sandbox.image,
            "output": render.output.strip()[-_OUTPUT_EXCERPT_LEN:],
            "probe": render.probe,
        }
        if not render.passed:
            return self._result(GateVerdict.FAIL, {**diagnostics, "error": render.problem})
        written = write_derived(self._root, RENDER_DIRECTORY, RENDER_FILENAME, render.video)
        return self._result(
            GateVerdict.PASS,
            {
                **diagnostics,
                "rendered": True,
                "video": str(written),
                "video_sha256": hashlib.sha256(render.video).hexdigest(),
            },
        )

    def _result(self, verdict: GateVerdict, diagnostics: dict[str, Any]) -> GateResult:
        return GateResult(gate_name=self.name, verdict=verdict, diagnostics=diagnostics)
