"""Metering boundary: every factory model call is reserved before dispatch and settled once.

Calls go through the real ADK `LiteLlm` model of a bound route to a loopback
counted-Responses toy server (`tests/responses_fixture.py`). Token counts are
the toy model's code points; USD amounts are policy quotes from the pinned
price data (reference capacity or a synthetic fixture fee), never spend.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from google.adk.models import LlmRequest
from google.genai import types

from apprentice.controls.authority import Authority, Cycle
from apprentice.controls.errors import ControlDeniedError
from apprentice.controls.footprint import Footprint
from apprentice.controls.policy import ControlPolicy
from apprentice.core.config import load_config
from apprentice.core.session_store import SessionStore
from apprentice.metering.client import UsageContractError
from apprentice.metering.pricing import load_price_snapshot
from apprentice.providers.factory import ModelRoute, resolve_route
from tests.responses_fixture import (
    REFERENCE_MODEL,
    ResponsesFixture,
    ResponsesServer,
    local_profile,
    paid_profile,
    toy_input_tokens,
    write_config,
)

if TYPE_CHECKING:
    from pathlib import Path

_EMPTY = Footprint(existing=())
_MARKER = "You are a curriculum designer."


@dataclass
class Env:
    store: SessionStore
    authority: Authority
    route: ModelRoute
    policy: ControlPolicy
    fixture: ResponsesFixture


@pytest.fixture
def fixture() -> ResponsesFixture:
    return ResponsesFixture(outputs={"discovery": "[1,2]"})


@pytest.fixture
def server(fixture: ResponsesFixture) -> Any:
    with ResponsesServer(fixture) as running:
        yield running


def _env(
    tmp_path: Path,
    server: ResponsesServer,
    *,
    name: str = "env",
    profile: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    **limits: Any,
) -> Env:
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    profile = profile or local_profile(root / "profile.json", max_output=64)
    base_url = "" if limits.get("backend") == "openai" else server.base_url
    config = load_config(
        write_config(root / "apprentice.toml", profile=profile, base_url=base_url, **limits)
    )
    store = SessionStore(store_dir=root / "sessions")
    return Env(
        store=store,
        authority=Authority.open(store.store_dir, _EMPTY),
        route=resolve_route(config.provider, transport=transport),
        policy=ControlPolicy.from_config(config),
        fixture=server.fixture,
    )


def _request(text: str = "Suggest one algorithm.", max_output: int | None = None) -> LlmRequest:
    return LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text=text)])],
        config=types.GenerateContentConfig(
            system_instruction=_MARKER, max_output_tokens=max_output
        ),
    )


async def _call(env: Env, cycle: Cycle, request: LlmRequest, role: str = "discovery") -> str:
    model = env.route.model(
        cycle, "discovery" if role == "discovery" else "artifact_generation", role
    )
    async for response in model.generate_content_async(request):
        return "".join(part.text or "" for part in response.content.parts)  # type: ignore[union-attr]
    raise AssertionError("no response")


def _entries(env: Env) -> list[dict[str, Any]]:
    conn = sqlite3.connect(env.store.store_dir / "controls" / "accounting.sqlite3")
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute("SELECT * FROM entries ORDER BY created_at")]
    conn.close()
    return rows


def _admission(env: Env) -> tuple[int, str]:
    """The held bound and binding control of the single admitted generation."""
    (entry,) = [e for e in _entries(env) if e["operation"] == "generate"]
    return entry["bound_tokens"], json.loads(entry["receipt"])["admission"]["binding_control"]


def _probe_input(tmp_path: Path, server: ResponsesServer, request: LlmRequest) -> int:
    """Learn the toy input size of `request` from a separate, generous installation."""
    env = _env(tmp_path, server, name="probe")
    with env.authority.begin_cycle("suggest", env.policy) as cycle:
        asyncio.run(_call(env, cycle, request))
    counted = [body for path, body in server.fixture.requests if path.endswith("input_tokens")]
    server.fixture.requests.clear()
    return toy_input_tokens(counted[-1])


class TestCallBoundary:
    def test_input_plus_cap_fills_the_call_ceiling_exactly(
        self, tmp_path: Path, server: ResponsesServer
    ) -> None:
        size = _probe_input(tmp_path, server, _request())
        env = _env(tmp_path, server, max_tokens_per_agent_call=size + 7)

        with env.authority.begin_cycle("suggest", env.policy) as cycle:
            asyncio.run(_call(env, cycle, _request()))

        (generation,) = env.fixture.generations()
        assert generation["max_output_tokens"] == 7
        assert generation["store"] is False and generation["service_tier"] == "default"

    def test_input_filling_the_call_ceiling_is_denied_before_generation(
        self, tmp_path: Path, server: ResponsesServer
    ) -> None:
        size = _probe_input(tmp_path, server, _request())
        env = _env(tmp_path, server, max_tokens_per_agent_call=size)

        with (
            pytest.raises(ControlDeniedError) as denied,
            env.authority.begin_cycle("suggest", env.policy) as cycle,
        ):
            asyncio.run(_call(env, cycle, _request()))

        assert denied.value.control == "budget.agent.max_tokens_per_agent_call"
        assert env.fixture.paths() == ["/v1/responses/input_tokens"]

    def test_explicit_request_cap_is_never_raised(
        self, tmp_path: Path, server: ResponsesServer
    ) -> None:
        env = _env(tmp_path, server)

        with env.authority.begin_cycle("suggest", env.policy) as cycle:
            asyncio.run(_call(env, cycle, _request(max_output=3)))

        assert env.fixture.generations()[0]["max_output_tokens"] == 3

    def test_counted_projection_is_exactly_what_generation_sends(
        self, tmp_path: Path, server: ResponsesServer
    ) -> None:
        env = _env(tmp_path, server)

        with env.authority.begin_cycle("suggest", env.policy) as cycle:
            asyncio.run(_call(env, cycle, _request()))

        (count, generation) = [body for _, body in env.fixture.requests]
        assert toy_input_tokens(count) == toy_input_tokens(generation)
        assert {k: v for k, v in generation.items() if k in count} == count

    def test_streaming_is_denied_before_any_reservation_or_request(
        self, tmp_path: Path, server: ResponsesServer
    ) -> None:
        env = _env(tmp_path, server)

        async def stream(cycle: Cycle) -> None:
            model = env.route.model(cycle, "discovery", "discovery")
            async for _ in model.generate_content_async(_request(), stream=True):
                pass

        with (
            pytest.raises(ControlDeniedError) as denied,
            env.authority.begin_cycle("suggest", env.policy) as cycle,
        ):
            asyncio.run(stream(cycle))

        assert denied.value.control == "provider.stream"
        assert env.fixture.paths() == []
        assert _entries(env) == []


class TestSettlement:
    def test_success_charges_input_plus_output_once_and_refunds_the_rest(
        self, tmp_path: Path, server: ResponsesServer
    ) -> None:
        env = _env(tmp_path, server)

        with env.authority.begin_cycle("suggest", env.policy) as cycle:
            asyncio.run(_call(env, cycle, _request()))

        count, generation = _entries(env)
        size = toy_input_tokens(env.fixture.generations()[0])
        assert (count["state"], count["charged_tokens"]) == ("settled", 0)
        assert generation["state"] == "settled"
        assert generation["bound_tokens"] == size + 64
        assert generation["charged_tokens"] == size + len("[1,2]")

    def test_reasoning_and_cached_subsets_are_not_counted_twice(
        self, tmp_path: Path, server: ResponsesServer, fixture: ResponsesFixture
    ) -> None:
        def with_subsets(usage: dict[str, Any]) -> dict[str, Any]:
            usage["input_tokens_details"] = {"cached_tokens": 10}
            usage["output_tokens_details"] = {"reasoning_tokens": 3}
            return usage

        fixture.usage_patch = with_subsets
        profile = local_profile(
            tmp_path / "reference.json",
            cost_policy="sdk-reference-capacity",
            reference=REFERENCE_MODEL,
            max_output=64,
        )
        env = _env(tmp_path, server, profile=profile)

        with env.authority.begin_cycle("suggest", env.policy) as cycle:
            asyncio.run(_call(env, cycle, _request()))

        generation = _entries(env)[-1]
        size = toy_input_tokens(env.fixture.generations()[0])
        price = load_price_snapshot().model(REFERENCE_MODEL)
        assert generation["charged_tokens"] == size + 5
        assert generation["charged_nanodollars"] == price.charge_nanodollars(size, 10, 5)
        assert generation["charged_nanodollars"] == (size - 10) * 2500 + 10 * 250 + 5 * 15000

    def test_above_bound_usage_is_charged_and_quarantines_the_profile(
        self, tmp_path: Path, server: ResponsesServer, fixture: ResponsesFixture
    ) -> None:
        def overrun(usage: dict[str, Any]) -> dict[str, Any]:
            usage["output_tokens"] += 1000
            usage["total_tokens"] += 1000
            return usage

        fixture.usage_patch = overrun
        env = _env(tmp_path, server)

        with (
            pytest.raises(UsageContractError),
            env.authority.begin_cycle("suggest", env.policy) as cycle,
        ):
            asyncio.run(_call(env, cycle, _request()))
        fixture.usage_patch = None
        with (
            pytest.raises(ControlDeniedError) as denied,
            env.authority.begin_cycle("suggest", env.policy) as cycle,
        ):
            asyncio.run(_call(env, cycle, _request()))

        generation = _entries(env)[1]
        size = toy_input_tokens(env.fixture.generations()[0])
        assert generation["charged_tokens"] == size + len("[1,2]") + 1000
        assert denied.value.control == "provider.accounting_profile_path"
        assert len(env.fixture.generations()) == 1

    def test_unqualified_echoed_model_keeps_the_full_bound_unknown(
        self, tmp_path: Path, server: ResponsesServer, fixture: ResponsesFixture
    ) -> None:
        fixture.echo_model = "some-other-model"
        env = _env(tmp_path, server)

        with (
            pytest.raises(UsageContractError),
            env.authority.begin_cycle("suggest", env.policy) as cycle,
        ):
            asyncio.run(_call(env, cycle, _request()))

        generation = _entries(env)[1]
        assert generation["state"] == "unknown"
        assert generation["charged_tokens"] is None
        assert env.authority.status()["quarantined_profiles"]

    def test_cancellation_after_dispatch_retains_the_full_bound(
        self, tmp_path: Path, server: ResponsesServer, fixture: ResponsesFixture
    ) -> None:
        fixture.gate = threading.Event()
        env = _env(tmp_path, server)

        async def cancel_in_flight(cycle: Cycle) -> None:
            task = asyncio.create_task(_call(env, cycle, _request()))
            await asyncio.get_running_loop().run_in_executor(None, fixture.arrived.wait)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        with env.authority.begin_cycle("suggest", env.policy) as cycle:
            asyncio.run(cancel_in_flight(cycle))
        fixture.gate.set()

        generation = _entries(env)[1]
        assert generation["state"] == "unknown"
        status = env.authority.status()
        unknown = [e for e in status["month_entries"] if e["state"] == "unknown"]
        assert unknown[0]["bound_tokens"] == generation["bound_tokens"]


class TestSharedScopes:
    def test_monthly_ceiling_persists_across_restart_at_equality(
        self, tmp_path: Path, server: ResponsesServer
    ) -> None:
        size = _probe_input(tmp_path, server, _request())
        first = size + len("[1,2]")
        env = _env(tmp_path, server, monthly_token_ceiling=first + size + 1)
        with env.authority.begin_cycle("suggest", env.policy) as cycle:
            asyncio.run(_call(env, cycle, _request()))
        env.authority.close()

        restarted = Authority.open(env.store.store_dir, _EMPTY)
        env.authority = restarted
        with restarted.begin_cycle("suggest", env.policy) as cycle:
            asyncio.run(_call(env, cycle, _request()))
        with (
            pytest.raises(ControlDeniedError) as denied,
            restarted.begin_cycle("suggest", env.policy) as cycle,
        ):
            asyncio.run(_call(env, cycle, _request()))

        assert [g["max_output_tokens"] for g in env.fixture.generations()] == [64, 1]
        assert denied.value.control == "budget.global.monthly_token_ceiling"

    def test_waits_for_an_active_hold_and_uses_its_refund(
        self, tmp_path: Path, server: ResponsesServer, fixture: ResponsesFixture
    ) -> None:
        size = _probe_input(tmp_path, server, _request())
        env = _env(tmp_path, server, max_tokens_per_stage=2 * size + 64)
        fixture.gate = threading.Event()

        async def two_roles(cycle: Cycle) -> None:
            first = asyncio.create_task(_call(env, cycle, _request(), role="instrumentation"))
            await asyncio.get_running_loop().run_in_executor(None, fixture.arrived.wait)
            second = asyncio.create_task(_call(env, cycle, _request(), role="visualization"))
            await asyncio.sleep(0.3)
            assert len(fixture.generations()) == 1
            fixture.gate.set()  # type: ignore[union-attr]
            await asyncio.gather(first, second)

        with env.authority.begin_cycle("build", env.policy) as cycle:
            asyncio.run(two_roles(cycle))

        caps = [g["max_output_tokens"] for g in fixture.generations()]
        assert caps == [64, 64 - len("[1,2]")]

    def test_committed_stage_usage_denies_instead_of_waiting(
        self, tmp_path: Path, server: ResponsesServer
    ) -> None:
        size = _probe_input(tmp_path, server, _request())
        env = _env(tmp_path, server, max_tokens_per_stage=2 * size + len("[1,2]"))

        with (
            pytest.raises(ControlDeniedError) as denied,
            env.authority.begin_cycle("build", env.policy) as cycle,
        ):
            asyncio.run(_call(env, cycle, _request(), role="instrumentation"))
            asyncio.run(_call(env, cycle, _request(), role="visualization"))

        assert denied.value.control == "budget.stage.max_tokens_per_stage"
        assert len(env.fixture.generations()) == 1

    def test_cycle_token_ceiling_bounds_input_plus_output_exactly(
        self, tmp_path: Path, server: ResponsesServer
    ) -> None:
        size = _probe_input(tmp_path, server, _request())
        env = _env(tmp_path, server, max_tokens_per_cycle=size + 7)

        with env.authority.begin_cycle("suggest", env.policy) as cycle:
            asyncio.run(_call(env, cycle, _request()))

        assert env.fixture.generations()[0]["max_output_tokens"] == 7
        assert _admission(env) == (size + 7, "budget.cycle.max_tokens_per_cycle")

    @pytest.mark.parametrize(
        ("role", "share_key", "pct"),
        [
            ("implementation", "implementation_budget_pct", 40),
            ("assessment", "tool_agent_budget_pct", 15),
        ],
    )
    def test_role_share_of_the_cycle_bounds_the_role_exactly(
        self, tmp_path: Path, server: ResponsesServer, role: str, share_key: str, pct: int
    ) -> None:
        size = _probe_input(tmp_path, server, _request())
        # The smallest cycle whose floor(cycle * pct / 100) share is size + 9.
        cycle_tokens = -(-(size + 9) * 100 // pct)
        env = _env(tmp_path, server, max_tokens_per_cycle=cycle_tokens, **{share_key: pct})

        with env.authority.begin_cycle("build", env.policy) as cycle:
            asyncio.run(_call(env, cycle, _request(), role=role))

        assert env.fixture.generations()[0]["max_output_tokens"] == 9
        assert _admission(env) == (size + 9, f"budget.agent.{share_key}")

    def test_reference_capacity_quote_meets_the_cycle_usd_ceiling_exactly(
        self, tmp_path: Path, server: ResponsesServer
    ) -> None:
        size = _probe_input(tmp_path, server, _request())
        exact = size * 2500 + 15000 * 9
        profile = local_profile(
            tmp_path / "reference.json",
            cost_policy="sdk-reference-capacity",
            reference=REFERENCE_MODEL,
            max_output=64,
        )
        caps = []
        for name, ceiling in (("equal", exact), ("short", exact - 1)):
            env = _env(
                tmp_path,
                server,
                name=name,
                profile=profile,
                max_cost_per_cycle_usd=str(Decimal(ceiling) / 10**9),
            )
            with env.authority.begin_cycle("suggest", env.policy) as cycle:
                asyncio.run(_call(env, cycle, _request()))
            caps.append(env.fixture.generations()[-1]["max_output_tokens"])

        assert caps == [9, 8]

    def test_reference_capacity_quote_meets_the_monthly_usd_ceiling_exactly(
        self, tmp_path: Path, server: ResponsesServer
    ) -> None:
        size = _probe_input(tmp_path, server, _request())
        exact = size * 2500 + 15000 * 9
        profile = local_profile(
            tmp_path / "reference.json",
            cost_policy="sdk-reference-capacity",
            reference=REFERENCE_MODEL,
            max_output=64,
        )
        caps, bindings = [], []
        for name, ceiling in (("equal", exact), ("short", exact - 1)):
            env = _env(
                tmp_path,
                server,
                name=name,
                profile=profile,
                monthly_cost_ceiling_usd=str(Decimal(ceiling) / 10**9),
                max_cost_per_cycle_usd="5.0",
            )
            with env.authority.begin_cycle("suggest", env.policy) as cycle:
                asyncio.run(_call(env, cycle, _request()))
            caps.append(env.fixture.generations()[-1]["max_output_tokens"])
            bindings.append(_admission(env)[1])

        assert caps == [9, 8]
        assert bindings == ["budget.global.monthly_cost_ceiling_usd"] * 2


class _ToLoopback(httpx.AsyncBaseTransport):
    """Internal test transport: delivers the paid route's requests to the loopback fixture."""

    def __init__(self, base_url: str) -> None:
        self._target = httpx.URL(base_url)
        self._inner = httpx.AsyncHTTPTransport(retries=0)
        self.hosts: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        request.url = request.url.copy_with(
            scheme=self._target.scheme, host=self._target.host, port=self._target.port
        )
        return await self._inner.handle_async_request(request)


class TestPaidRoute:
    def test_counter_fee_is_reserved_before_counting_and_kept_when_generation_is_denied(
        self, tmp_path: Path, server: ResponsesServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "offline-test-not-a-key")
        transport = _ToLoopback(server.base_url)
        profile = paid_profile(tmp_path / "paid.json", fee_usd="0.000001")
        env = _env(
            tmp_path,
            server,
            profile=profile,
            transport=transport,
            backend="openai",
            model="openai/gpt-5.4",
            max_tokens_per_agent_call=10,
        )
        env_base = env.route.endpoint

        with (
            pytest.raises(ControlDeniedError),
            env.authority.begin_cycle("suggest", env.policy) as cycle,
        ):
            asyncio.run(_call(env, cycle, _request()))

        (count,) = _entries(env)
        assert env_base == "https://api.openai.com/v1"
        assert transport.hosts == ["api.openai.com"]
        assert env.fixture.paths() == ["/v1/responses/input_tokens"]
        assert (count["state"], count["charged_nanodollars"]) == ("settled", 1000)

    def test_unaffordable_counter_fee_denies_before_counting(
        self, tmp_path: Path, server: ResponsesServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "offline-test-not-a-key")
        profile = paid_profile(tmp_path / "paid.json", fee_usd="0.000001")
        env = _env(
            tmp_path,
            server,
            profile=profile,
            transport=_ToLoopback(server.base_url),
            backend="openai",
            model="openai/gpt-5.4",
            max_cost_per_cycle_usd="0.000000999",
        )

        with (
            pytest.raises(ControlDeniedError) as denied,
            env.authority.begin_cycle("suggest", env.policy) as cycle,
        ):
            asyncio.run(_call(env, cycle, _request()))

        assert denied.value.control == "budget.cycle.max_cost_per_cycle_usd"
        assert env.fixture.paths() == []
