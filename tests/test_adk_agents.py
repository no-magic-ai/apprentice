"""ADK agents: implementation attempts, discovery tools and programmatic review."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from apprentice.agents.discovery import (
    check_duplicate,
    load_catalog,
    validate_name,
)
from apprentice.agents.implementation import build_implementation_agent
from apprentice.agents.review import build_review_agent
from apprentice.controls.authority import Authority
from apprentice.controls.footprint import Footprint
from apprentice.controls.policy import ControlPolicy
from apprentice.core.config import load_config
from apprentice.providers.factory import resolve_route
from tests.conftest import fixture_outputs
from tests.responses_fixture import ResponsesFixture, ResponsesServer, local_profile, write_config

if TYPE_CHECKING:
    from collections.abc import Iterator

    from apprentice.controls.authority import Cycle
    from apprentice.core.artifacts import RunScope
    from apprentice.providers.factory import ModelRoute


async def _run(agent: Any, state: dict[str, Any] | None = None) -> dict[str, Any]:
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    service = InMemorySessionService()  # type: ignore[no-untyped-call]
    runner = Runner(agent=agent, app_name="t", session_service=service)
    session = await service.create_session(app_name="t", user_id="u", state=state or {})
    message = types.Content(role="user", parts=[types.Part(text="Build selection sort.")])
    async for _ in runner.run_async(user_id="u", session_id=session.id, new_message=message):
        pass
    stored = await service.get_session(app_name="t", user_id="u", session_id=session.id)
    assert stored is not None
    return dict(stored.state)


@pytest.fixture
def toy(tmp_path: Path) -> Iterator[tuple[ResponsesFixture, ModelRoute, Cycle]]:
    fixture = ResponsesFixture()
    with ResponsesServer(fixture) as server:
        config = load_config(
            write_config(
                tmp_path / "apprentice.toml",
                profile=local_profile(tmp_path / "profile.json"),
                base_url=server.base_url,
            )
        )
        installation = tmp_path / "installation"
        installation.mkdir()
        authority = Authority.open(installation, Footprint(existing=()))
        with authority.begin_cycle("build", ControlPolicy.from_config(config)) as cycle:
            yield fixture, resolve_route(config.provider), cycle


@pytest.mark.usefixtures("judged_execution")
class TestImplementationLoop:
    def test_valid_first_draft_ends_the_loop_after_one_call(
        self, scope: RunScope, toy: tuple[ResponsesFixture, ModelRoute, Cycle]
    ) -> None:
        fixture, route, cycle = toy
        fixture.outputs = fixture_outputs()
        agent = build_implementation_agent(
            route.model(cycle, "implementation", "implementation"), scope.work_root, max_attempts=3
        )

        state = asyncio.run(_run(agent))

        assert len(fixture.generations()) == 1
        assert state["validation_feedback"] == ""
        assert Path(state["implementation_path"]).read_text() == fixture.outputs["drafter"]

    def test_failing_drafts_run_the_total_attempts_including_the_first(
        self, scope: RunScope, toy: tuple[ResponsesFixture, ModelRoute, Cycle]
    ) -> None:
        fixture, route, cycle = toy
        fixture.outputs = fixture_outputs(failing_implementation=True)
        agent = build_implementation_agent(
            route.model(cycle, "implementation", "implementation"), scope.work_root, max_attempts=2
        )

        state = asyncio.run(_run(agent))

        _first, second = fixture.generations()
        assert "fixture" in second["instructions"]
        assert "implementation_path" not in state


class TestDiscoveryTools:
    def test_load_catalog_returns_dict(self) -> None:
        result = load_catalog()
        assert isinstance(result, dict)
        assert "algorithms" in result
        assert "all_names" in result
        assert isinstance(result["algorithms"], list)

    def test_check_duplicate_known_name(self) -> None:
        result = check_duplicate("binary_search")
        assert result["is_duplicate"] is True

    def test_check_duplicate_unknown_name(self) -> None:
        result = check_duplicate("totally_unique_algo_xyz")
        assert result["is_duplicate"] is False

    def test_validate_name_valid(self) -> None:
        result = validate_name("merge_sort")
        assert result["valid"] is True
        assert result["normalized"] == "merge_sort"

    def test_validate_name_normalizes(self) -> None:
        result = validate_name("Merge-Sort")
        assert result["normalized"] == "merge_sort"
        assert result["valid"] is True


class TestProgrammaticReview:
    def test_review_validates_every_artifact_without_a_model(self, scope: RunScope) -> None:
        outputs = fixture_outputs()
        state = {
            "generated_code": outputs["drafter"],
            "instrumented_code": outputs["instrumentation"],
            "manim_scene_code": outputs["visualization"],
            "anki_deck_content": outputs["assessment"],
        }

        final = asyncio.run(_run(build_review_agent(scope.work_root, scope.algorithm), state))

        assert final["review_verdict"] == "passed"

    def test_review_reports_an_invalid_artifact(self, scope: RunScope) -> None:
        outputs = fixture_outputs()
        state = {
            "generated_code": outputs["drafter"],
            "instrumented_code": outputs["instrumentation"],
            "manim_scene_code": outputs["visualization"],
            "anki_deck_content": "one column only\n",
        }

        final = asyncio.run(_run(build_review_agent(scope.work_root, scope.algorithm), state))

        assert final["review_verdict"].startswith("failed: ")
