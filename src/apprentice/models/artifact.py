"""ArtifactBundle model — tracks generated artifacts per work item."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Self


@dataclass
class ArtifactBundle:
    """Collection of generated artifacts for a single algorithm."""

    id: str
    work_item_id: str
    revision_number: int = 1
    parent_bundle_id: str | None = None
    implementation_path: str = ""
    instrumented_path: str = ""
    manim_scene_path: str = ""
    anki_deck_path: str = ""
    readme_section: str = ""
    template_version: str = ""
    pr_url: str = ""
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "work_item_id": self.work_item_id,
            "revision_number": self.revision_number,
            "parent_bundle_id": self.parent_bundle_id,
            "implementation_path": self.implementation_path,
            "instrumented_path": self.instrumented_path,
            "manim_scene_path": self.manim_scene_path,
            "anki_deck_path": self.anki_deck_path,
            "readme_section": self.readme_section,
            "template_version": self.template_version,
            "pr_url": self.pr_url,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            id=data["id"],
            work_item_id=data["work_item_id"],
            revision_number=data.get("revision_number", 1),
            parent_bundle_id=data.get("parent_bundle_id"),
            implementation_path=data.get("implementation_path", ""),
            instrumented_path=data.get("instrumented_path", ""),
            manim_scene_path=data.get("manim_scene_path", ""),
            anki_deck_path=data.get("anki_deck_path", ""),
            readme_section=data.get("readme_section", ""),
            template_version=data.get("template_version", ""),
            pr_url=data.get("pr_url", ""),
            created_at=datetime.fromisoformat(data["created_at"]),
        )
