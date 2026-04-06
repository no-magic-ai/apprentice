"""Prompt template loading and rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import BaseLoader, Environment, StrictUndefined, TemplateError

_DEFAULT_PROMPTS_DIR = Path(__file__).parent

_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"name", "version", "system_prompt", "user_prompt_template", "variables"}
)


@dataclass
class PromptTemplate:
    """Parsed and validated prompt template.

    Attributes:
        name: Template identifier (must match the filename stem).
        version: Semantic version string.
        system_prompt: Static system message sent before the user turn.
        user_prompt_template: Jinja2 template string for the user turn.
        variables: Required variable names that must be supplied at render time.
    """

    name: str
    version: str
    system_prompt: str
    user_prompt_template: str
    variables: list[str]


def load_prompt(name: str, prompts_dir: Path | None = None) -> PromptTemplate:
    """Load and validate a prompt template from a YAML file.

    Args:
        name: Stem of the YAML file to load (e.g. ``"implementation"``).
        prompts_dir: Directory that contains the YAML files.
                     Defaults to the ``prompts/`` package directory.

    Returns:
        A fully validated :class:`PromptTemplate`.

    Raises:
        FileNotFoundError: If no YAML file named ``<name>.yaml`` exists.
        ValueError: If the YAML file is missing required fields or has wrong types.
    """
    directory = prompts_dir if prompts_dir is not None else _DEFAULT_PROMPTS_DIR
    yaml_path = directory / f"{name}.yaml"

    if not yaml_path.exists():
        raise FileNotFoundError(f"Prompt template not found: {yaml_path}")

    with yaml_path.open(encoding="utf-8") as fh:
        raw: Any = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError(f"Expected a YAML mapping in {yaml_path}, got {type(raw).__name__}")

    missing = _REQUIRED_FIELDS - raw.keys()
    if missing:
        raise ValueError(f"Prompt template {yaml_path} missing required fields: {sorted(missing)}")

    variables = raw["variables"]
    if not isinstance(variables, list) or not all(isinstance(v, str) for v in variables):
        raise ValueError(
            f"'variables' in {yaml_path} must be a list of strings, got: {variables!r}"
        )

    for field in ("name", "version", "system_prompt", "user_prompt_template"):
        if not isinstance(raw[field], str):
            raise ValueError(
                f"Field '{field}' in {yaml_path} must be a string, got: {type(raw[field]).__name__}"
            )

    return PromptTemplate(
        name=raw["name"],
        version=raw["version"],
        system_prompt=raw["system_prompt"],
        user_prompt_template=raw["user_prompt_template"],
        variables=variables,
    )


def render_prompt(
    template: PromptTemplate,
    variables: dict[str, Any],
) -> tuple[str, str]:
    """Render the user prompt template with the provided variables.

    Args:
        template: A :class:`PromptTemplate` returned by :func:`load_prompt`.
        variables: Key-value pairs to substitute into the template.
                   All names listed in ``template.variables`` must be present.

    Returns:
        A ``(system_prompt, rendered_user_prompt)`` tuple.

    Raises:
        ValueError: If any required variable is absent from ``variables``
                    or if the Jinja2 template fails to render.
    """
    missing = [v for v in template.variables if v not in variables]
    if missing:
        raise ValueError(
            f"Missing required template variables for '{template.name}': {sorted(missing)}"
        )

    env = Environment(
        loader=BaseLoader(),
        undefined=StrictUndefined,
        autoescape=False,
    )

    try:
        jinja_template = env.from_string(template.user_prompt_template)
        rendered = jinja_template.render(**variables)
    except TemplateError as exc:
        raise ValueError(f"Failed to render prompt '{template.name}': {exc}") from exc

    return template.system_prompt, rendered
