"""Metered Responses transport behind every factory model call.

ADK's `LiteLlm` hands each request to its `llm_client`; this client replaces
the default one for every role of a controlled cycle. One request is:

1. Reject streaming, unsupported fields, non-function tools and non-text
   input before any reservation.
2. Transform the chat request into Responses input once and freeze the
   complete input projection.
3. Reserve the counting fee bound, persist dispatch intent, count that exact
   projection at `/responses/input_tokens` and keep the fee charged at its
   bound (a count is a size measurement, not billed usage).
4. Reserve counted input plus the largest admissible output and its
   worst-case quote, persist dispatch intent and send the same projection to
   `/responses` with that `max_output_tokens`, `store=false` and the
   standard tier.
5. Validate the raw echoed model, tier and complete usage before any SDK
   conversion, and settle the entry exactly once.

The OpenAI client is created per call with SDK retries disabled, an httpx
transport without retries, no environment proxies and no redirects. After
dispatch, any failure, cancellation or missing usage keeps the full bound
held as unknown — never zero.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from google.adk.models.lite_llm import LiteLLMClient

from apprentice.controls.errors import ControlDeniedError
from apprentice.controls.ledger import (
    Admission,
    Attempt,
    GenerationLimits,
    admit_generation,
    mark_dispatched,
    release,
    reserve_counter,
    retain_unknown,
    settle,
)
from apprentice.metering.profile import STANDARD_TIER

if TYPE_CHECKING:
    from apprentice.controls.authority import Cycle
    from apprentice.metering.profile import AccountingProfile
    from apprentice.providers.factory import ModelRoute

COUNTED_FIELDS = frozenset(
    {
        "conversation",
        "input",
        "instructions",
        "model",
        "parallel_tool_calls",
        "previous_response_id",
        "reasoning",
        "text",
        "tool_choice",
        "tools",
        "truncation",
    }
)
_GENERATION_ONLY_FIELDS = frozenset({"max_output_tokens", "temperature", "top_p"})
_SDK_ONLY_FIELDS = frozenset({"litellm_logging_obj", "client"})
_TEXT_PARTS = frozenset({"input_text", "output_text"})
_INPUT_ITEMS = frozenset({"function_call", "function_call_output"})
_REQUEST_TIMEOUT_SECONDS = 600.0


class UsageContractError(Exception):
    """A dispatched request returned no complete, consistent usage or an unqualified echo."""


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _require_supported(payload: dict[str, Any]) -> None:
    unsupported = payload.keys() - COUNTED_FIELDS - _GENERATION_ONLY_FIELDS
    if unsupported:
        raise ControlDeniedError(
            "provider.request", f"request fields {sorted(unsupported)} cannot be counted"
        )
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ControlDeniedError("provider.request", f"unsupported non-function tool {tool!r}")
    for item in payload.get("input") or []:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind in _INPUT_ITEMS:
            continue
        if kind != "message":
            raise ControlDeniedError("provider.request", f"unsupported input item {kind!r}")
        content = item.get("content")
        parts = content if isinstance(content, list) else []
        if not isinstance(content, (list, str)) or any(
            not isinstance(p, dict) or p.get("type") not in _TEXT_PARTS for p in parts
        ):
            raise ControlDeniedError("provider.request", "only text message input is supported")


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class MeteredResponsesClient(LiteLLMClient):
    """The bound accounting client of one cycle, stage and role."""

    def __init__(self, route: ModelRoute, cycle: Cycle, stage: str, role: str) -> None:
        self.route = route
        self.cycle = cycle
        self.stage = stage
        self.role = role

    def _http(self) -> httpx.AsyncClient:
        transport = self.route.transport or httpx.AsyncHTTPTransport(retries=0)
        return httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

    def _prepare(
        self, model: str, messages: list[Any], tools: Any, kwargs: dict[str, Any]
    ) -> tuple[dict[str, Any], tuple[Any, ...]]:
        from litellm import LiteLLMLoggingObj
        from litellm.completion_extras.litellm_responses_transformation.transformation import (
            LiteLLMResponsesTransformationHandler,
        )

        if model != self.route.model_string:
            raise ControlDeniedError(
                "provider.model",
                f"model {model!r} is not the bound route {self.route.model_string!r}",
            )
        optional = {key: value for key, value in kwargs.items() if value is not None}
        if tools:
            optional["tools"] = tools
        logger = LiteLLMLoggingObj(
            model=self.route.profile.requested_model,
            messages=messages,
            stream=False,
            call_type="acompletion",
            start_time=datetime.now(tz=UTC),
            litellm_call_id=self.cycle.cycle_id,
            function_id=self.cycle.cycle_id,
        )
        handler = LiteLLMResponsesTransformationHandler()  # type: ignore[no-untyped-call]
        prepared = handler.transform_request(
            model=self.route.profile.requested_model,
            messages=messages,
            optional_params=optional,
            litellm_params={},
            headers={},
            litellm_logging_obj=logger,
        )
        payload = {k: v for k, v in prepared.items() if k not in _SDK_ONLY_FIELDS}
        payload["model"] = self.route.profile.requested_model
        _require_supported(payload)
        return payload, (logger, prepared, optional, messages)

    async def acompletion(self, model: str, messages: list[Any], tools: Any, **kwargs: Any) -> Any:
        """Run one metered, non-streaming completion and return the SDK response."""
        if kwargs.pop("stream", False):
            raise ControlDeniedError(
                "provider.stream", "streaming is not metered; no request was reserved or sent"
            )
        kwargs.pop("stream_options", None)
        payload, transform = self._prepare(model, messages, tools, kwargs)
        requested_cap = payload.pop("max_output_tokens", None)
        projection = {k: v for k, v in payload.items() if k in COUNTED_FIELDS}
        projection_sha = _digest(projection)
        profile = self.route.profile
        fee = profile.counter_fee
        request_bytes = len(json.dumps(projection, separators=(",", ":"), default=str).encode())
        if fee.max_request_bytes is not None and request_bytes > fee.max_request_bytes:
            raise ControlDeniedError(
                "provider.accounting_profile_path",
                f"counting request of {request_bytes} bytes exceeds the qualified fee envelope "
                f"{fee.max_request_bytes}",
            )
        attempt = Attempt(self.cycle, self.stage, self.role)
        identity = {
            "attempt_id": attempt.attempt_id,
            "profile_sha256": profile.sha256,
            "price_data_sha256": self.route.price_sha256,
            "requested_model": profile.requested_model,
            "input_projection_sha256": projection_sha,
        }
        async with self._http() as http, self.route.openai_client(http) as client:
            input_tokens = await self._count(attempt, client, projection, request_bytes, identity)
            limits = GenerationLimits(
                profile_sha256=profile.sha256,
                basis=profile.cost_policy,
                price=profile.price,
                max_output_tokens=profile.max_output_tokens,
                context_tokens=None if profile.hosted else profile.max_input_tokens,
                max_input_tokens=profile.max_input_tokens if profile.hosted else None,
                requested_cap=requested_cap if isinstance(requested_cap, int) else None,
            )
            admission = await admit_generation(
                attempt, input_tokens=input_tokens, limits=limits, receipt=identity
            )
            body = await self._generate(attempt, client, admission.entry_id, payload, admission.cap)
        self._settle(attempt, admission, input_tokens, body)
        return _convert(self.route.profile.requested_model, body, transform)

    async def _count(
        self,
        attempt: Attempt,
        client: Any,
        projection: dict[str, Any],
        request_bytes: int,
        identity: dict[str, Any],
    ) -> int:
        profile = self.route.profile
        entry = await reserve_counter(
            attempt,
            profile_sha256=profile.sha256,
            basis=profile.cost_policy,
            fee_nanodollars=profile.counter_fee.nanodollars,
            receipt={**identity, "request_bytes": request_bytes},
        )
        mark_dispatched(attempt, entry, {"at": datetime.now(tz=UTC).isoformat()})
        try:
            raw = await client.responses.input_tokens.with_raw_response.count(**projection)
            body = raw.http_response.json()
        except BaseException as exc:
            retain_unknown(attempt, entry, {"error": repr(exc)})
            raise
        counted = _int(body.get("input_tokens")) if isinstance(body, dict) else None
        settle(
            attempt,
            entry,
            tokens=0,
            nanodollars=profile.counter_fee.nanodollars,
            receipt={"raw": body, "basis": "fee upper bound; counts are not billed usage"},
            quarantine=None if counted is not None else "counting response has no input_tokens",
        )
        if counted is None:
            raise UsageContractError(f"counting response has no input_tokens: {body!r}")
        return counted

    async def _generate(
        self, attempt: Attempt, client: Any, entry: str, payload: dict[str, Any], cap: int
    ) -> dict[str, Any]:
        request = {
            **payload,
            "max_output_tokens": cap,
            "store": False,
            "service_tier": STANDARD_TIER,
        }
        try:
            mark_dispatched(attempt, entry, {"at": datetime.now(tz=UTC).isoformat()})
        except BaseException:
            release(attempt, entry, {"reason": "dispatch intent was not persisted"})
            raise
        try:
            raw = await client.responses.with_raw_response.create(**request)
            body = raw.http_response.json()
        except BaseException as exc:
            retain_unknown(attempt, entry, {"error": repr(exc)})
            raise
        if not isinstance(body, dict):
            retain_unknown(attempt, entry, {"raw": body})
            raise UsageContractError("generation response is not a JSON object")
        return body

    def _settle(
        self, attempt: Attempt, admission: Admission, counted: int, body: dict[str, Any]
    ) -> None:
        """Settle the generation once from the raw body, before any SDK conversion.

        Unverifiable responses (unqualified echo or tier, missing usage) keep
        the full bound as unknown; inconsistent or above-bound usage is charged
        at no less than the bound. Both quarantine the profile and fail the call.
        """
        profile = self.route.profile
        problem = _unverifiable(body, profile)
        if problem is not None:
            retain_unknown(attempt, admission.entry_id, {"raw": body}, quarantine=problem)
            raise UsageContractError(problem)
        usage = body["usage"]
        reported_in = usage["input_tokens"]
        output = usage["output_tokens"]
        cached = _subset(usage, "input_tokens_details", "cached_tokens")
        tokens = reported_in + output
        nanos = (
            profile.price.charge_nanodollars(reported_in, min(cached or 0, reported_in), output)
            if profile.price
            else 0
        )
        problem = _inconsistency(usage, profile, counted, admission.cap)
        if problem is None and (
            tokens > admission.bound_tokens or nanos > admission.bound_nanodollars
        ):
            problem = f"usage {tokens} tokens / {nanos} nanodollars exceeds the reserved bound"
        receipt = {"raw_usage": usage, "model": body.get("model"), "status": body.get("status")}
        if problem is None:
            settle(attempt, admission.entry_id, tokens=tokens, nanodollars=nanos, receipt=receipt)
            return
        settle(
            attempt,
            admission.entry_id,
            tokens=max(tokens, admission.bound_tokens),
            nanodollars=max(nanos, admission.bound_nanodollars),
            receipt=receipt,
            quarantine=problem,
        )
        raise UsageContractError(problem)


def _subset(usage: dict[str, Any], parent: str, key: str) -> int | None:
    details = usage.get(parent)
    return _int(details.get(key)) if isinstance(details, dict) else None


def _unverifiable(body: dict[str, Any], profile: AccountingProfile) -> str | None:
    if body.get("model") not in profile.response_models:
        return f"echoed model {body.get('model')!r} is not a qualified model of the profile"
    if profile.hosted and body.get("service_tier") != STANDARD_TIER:
        return f"echoed service tier {body.get('service_tier')!r} is not {STANDARD_TIER!r}"
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return "response has no usage"
    if any(
        _int(usage.get(key)) is None for key in ("input_tokens", "output_tokens", "total_tokens")
    ):
        return f"usage is incomplete: {usage!r}"
    for parent, key, applies in (
        ("input_tokens_details", "cached_tokens", profile.cached_input_subset),
        ("output_tokens_details", "reasoning_tokens", profile.reasoning_output_subset),
    ):
        if applies and _subset(usage, parent, key) is None:
            return f"usage has no {parent}.{key}"
    return None


def _inconsistency(
    usage: dict[str, Any], profile: AccountingProfile, counted: int, cap: int
) -> str | None:
    reported_in = usage["input_tokens"]
    output = usage["output_tokens"]
    if usage["total_tokens"] != reported_in + output or reported_in != counted:
        return f"usage {usage!r} contradicts the counted input {counted}"
    for parent, key, whole, applies in (
        ("input_tokens_details", "cached_tokens", reported_in, profile.cached_input_subset),
        ("output_tokens_details", "reasoning_tokens", output, profile.reasoning_output_subset),
    ):
        value = _subset(usage, parent, key)
        if value is not None and (value > whole or (not applies and value != 0)):
            return f"usage {parent}.{key}={value} is not a subset the profile declares"
    if output > cap:
        return f"output {output} exceeds the transported cap {cap}"
    return None


def _convert(model: str, body: dict[str, Any], transform: tuple[Any, ...]) -> Any:
    from litellm.completion_extras.litellm_responses_transformation.transformation import (
        LiteLLMResponsesTransformationHandler,
    )
    from litellm.types.llms.openai import ResponsesAPIResponse
    from litellm.types.utils import ModelResponse

    logger, prepared, optional, messages = transform
    handler = LiteLLMResponsesTransformationHandler()  # type: ignore[no-untyped-call]
    return handler.transform_response(
        model=model,
        raw_response=ResponsesAPIResponse(**body),
        model_response=ModelResponse(),
        logging_obj=logger,
        request_data=prepared,
        messages=messages,
        optional_params=optional,
        litellm_params={},
        encoding=None,
    )
