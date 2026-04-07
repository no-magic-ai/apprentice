"""Packaging Agent — ADK LlmAgent that creates coordinated PRs in multiple repos."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.adk.agents import LlmAgent

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm

_TIER_DIRS: dict[int, str] = {
    1: "01-foundations",
    2: "02-alignment",
    3: "03-systems",
    4: "04-agents",
}

_INSTRUCTION = """\
You are a release engineer for the no-magic educational project.
Your task is to package algorithm artifacts into coordinated pull requests
across the no-magic and no-magic-viz repositories.

Artifact paths are available in session state:
- Implementation: {implementation_path}
- Instrumented: {instrumented_path}
- Manim scene: {manim_scene_path}
- Anki deck: {anki_deck_path}
- Algorithm name: {algorithm_name}
- Tier: {algorithm_tier}

Steps:
1. Use place_file to copy the implementation to no-magic/{tier_dir}/micro{name}.py
2. Use place_file to copy the manim scene to no-magic-viz/scenes/scene_micro{name}.py
3. Use create_branch to create feature branches in both repos
4. Use open_pr to create PRs in both repos with cross-references

Naming convention: all files use the 'micro' prefix (e.g., microquicksort.py).
"""


def clone_repo(repo_url: str, target_dir: str) -> dict[str, Any]:
    """Clone a GitHub repository to a local directory.

    Args:
        repo_url: GitHub repository URL (e.g. "https://github.com/no-magic-ai/no-magic").
        target_dir: Local directory path to clone into.

    Returns:
        Dict with 'success' bool and 'path' to the cloned directory.
    """
    target = Path(target_dir)
    if target.exists():
        return {"success": True, "path": str(target), "note": "Directory already exists"}

    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(target)],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return {"success": True, "path": str(target)}
    except subprocess.CalledProcessError as exc:
        return {"success": False, "path": "", "error": exc.stderr[:300]}
    except subprocess.TimeoutExpired:
        return {"success": False, "path": "", "error": "Clone timed out after 60s"}


def create_branch(repo_dir: str, branch_name: str) -> dict[str, Any]:
    """Create and checkout a new git branch in a repository.

    Args:
        repo_dir: Path to the local repository.
        branch_name: Name of the branch to create (e.g. "feat/microquicksort").

    Returns:
        Dict with 'success' bool.
    """
    try:
        subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return {"success": True, "branch": branch_name}
    except subprocess.CalledProcessError as exc:
        return {"success": False, "error": exc.stderr[:300]}


def place_file(source_path: str, dest_path: str) -> dict[str, Any]:
    """Copy a file to a destination path, creating directories as needed.

    Args:
        source_path: Absolute path to the source file.
        dest_path: Absolute path to the destination file.

    Returns:
        Dict with 'success' bool and 'path' to the placed file.
    """
    src = Path(source_path)
    dst = Path(dest_path)

    if not src.exists():
        return {"success": False, "error": f"Source file not found: {source_path}"}

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {"success": True, "path": str(dst)}


def open_pr(
    repo_dir: str,
    title: str,
    body: str,
    base_branch: str = "main",
) -> dict[str, Any]:
    """Stage all changes, commit, push, and open a pull request via gh CLI.

    Args:
        repo_dir: Path to the local repository.
        title: PR title.
        body: PR body/description.
        base_branch: Target branch for the PR.

    Returns:
        Dict with 'success' bool and 'pr_url' string.
    """
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", title],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

        result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--title",
                title,
                "--body",
                body,
                "--base",
                base_branch,
            ],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        pr_url = result.stdout.strip()
        return {"success": True, "pr_url": pr_url}
    except subprocess.CalledProcessError as exc:
        return {"success": False, "pr_url": "", "error": exc.stderr[:300]}
    except FileNotFoundError:
        return {"success": False, "pr_url": "", "error": "gh CLI not found"}


def render_preview(manim_scene_path: str, output_path: str) -> dict[str, Any]:
    """Render a Manim scene to GIF for PR preview.

    Args:
        manim_scene_path: Path to the Manim scene Python file.
        output_path: Path where the GIF should be saved.

    Returns:
        Dict with 'success' bool and 'path' to the rendered GIF.
    """
    try:
        result = subprocess.run(
            [
                "manim",
                "render",
                "-ql",
                "--format",
                "gif",
                "-o",
                output_path,
                manim_scene_path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return {"success": True, "path": output_path}
        return {"success": False, "path": "", "error": result.stderr[:300]}
    except FileNotFoundError:
        return {
            "success": False,
            "path": "",
            "error": "manim CLI not found — install manim for preview rendering",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "path": "", "error": "Render timed out after 120s"}


def get_tier_directory(tier: int) -> dict[str, str]:
    """Get the no-magic repository directory name for an algorithm tier.

    Args:
        tier: Algorithm tier (1-4).

    Returns:
        Dict with 'tier_dir' name.
    """
    tier_dir = _TIER_DIRS.get(tier, _TIER_DIRS[2])
    return {"tier_dir": tier_dir}


def build_packaging_agent(model: LiteLlm) -> LlmAgent:
    """Build an ADK LlmAgent for multi-repo PR packaging.

    Creates coordinated PRs in no-magic and no-magic-viz repositories,
    placing artifacts in correct directories with micro prefix.

    Args:
        model: LiteLlm model instance.

    Returns:
        A configured LlmAgent with file management and git tools.
    """
    return LlmAgent(
        name="packaging",
        model=model,
        instruction=_INSTRUCTION,
        tools=[clone_repo, create_branch, place_file, open_pr, render_preview, get_tier_directory],
        output_key="pr_urls",
        description="Packages algorithm artifacts into coordinated PRs across repositories.",
    )
