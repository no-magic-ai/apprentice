"""Loopback counted-Responses server: a toy non-hosted model for offline metering tests.

The toy model `m12-owned-codepoint-fixture` tokenizes the complete canonical
input projection and its output as Unicode code points. It is not a GPT
model, its counts are not OpenAI tokens and nothing it serves is billed.
Outputs are trusted hand-written fixtures selected by the agent's
instruction; whatever it serves is provider output and is never executed.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

TOY_MODEL = "m12-owned-codepoint-fixture"
REFERENCE_MODEL = "gpt-5.4-2026-03-05"
_COUNTED = (
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
)
ROLE_MARKERS = {
    "expert algorithm implementer": "drafter",
    "algorithm instrumentation": "instrumentation",
    "expert Manim animator": "visualization",
    "spaced-repetition card author": "assessment",
    "curriculum designer": "discovery",
}


def projection(body: dict[str, Any]) -> dict[str, Any]:
    return {k: body[k] for k in _COUNTED if body.get(k) is not None}


def toy_input_tokens(body: dict[str, Any]) -> int:
    """One token per code point of the canonical complete input projection."""
    return len(
        json.dumps(projection(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )


def role_of(body: dict[str, Any]) -> str:
    instructions = str(body.get("instructions") or "")
    return next((r for marker, r in ROLE_MARKERS.items() if marker in instructions), "unknown")


@dataclass
class ResponsesFixture:
    """Scripted loopback server state; every request is recorded in arrival order.

    `outputs` maps a role to the text it answers. `usage_patch` may rewrite
    the usage object of a generation response; `echo_model` overrides the
    echoed model; `gate` (when set) holds every generation until released.
    """

    outputs: dict[str, str] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    usage_patch: Any = None
    echo_model: str | None = None
    gate: threading.Event | None = None
    arrived: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def paths(self) -> list[str]:
        with self.lock:
            return [path for path, _ in self.requests]

    def generations(self) -> list[dict[str, Any]]:
        with self.lock:
            return [body for path, body in self.requests if path == "/v1/responses"]

    def respond(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self.lock:
            self.requests.append((path, body))
        if path == "/v1/responses/input_tokens":
            return 200, {"object": "response.input_tokens", "input_tokens": toy_input_tokens(body)}
        if path != "/v1/responses":
            return 404, {"error": {"message": f"unknown path {path}"}}
        self.arrived.set()
        if self.gate is not None:
            self.gate.wait()
        text = self.outputs.get(role_of(body), "")
        cap = int(body["max_output_tokens"])
        served = text[:cap]
        truncated = len(text) > cap
        usage: dict[str, Any] = {
            "input_tokens": toy_input_tokens(body),
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": len(served),
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": toy_input_tokens(body) + len(served),
        }
        if self.usage_patch is not None:
            usage = self.usage_patch(usage)
        return 200, {
            "id": f"resp_fixture_{len(self.requests)}",
            "object": "response",
            "created_at": 1,
            "status": "incomplete" if truncated else "completed",
            "model": self.echo_model or body["model"],
            "service_tier": "default",
            "error": None,
            "incomplete_details": {"reason": "max_output_tokens"} if truncated else None,
            "instructions": body.get("instructions"),
            "max_output_tokens": cap,
            "output": [
                {
                    "id": f"msg_fixture_{len(self.requests)}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": served, "annotations": []}],
                }
            ],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": body.get("tools", []),
            "temperature": 1.0,
            "top_p": 1.0,
            "truncation": "disabled",
            "usage": usage,
        }


class _Handler(BaseHTTPRequestHandler):
    fixture: ResponsesFixture

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        status, payload = self.server.fixture.respond(self.path, body)  # type: ignore[attr-defined]
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class ResponsesServer:
    """Runs a `ResponsesFixture` on an ephemeral loopback port."""

    def __init__(self, fixture: ResponsesFixture) -> None:
        self.fixture = fixture
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fixture = fixture  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> ResponsesServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.fixture.gate is not None:
            self.fixture.gate.set()
        self._server.shutdown()
        self._server.server_close()


def provenance() -> dict[str, str]:
    return {
        "source": "private offline test fixture (not a vendor price or capability source)",
        "sha256": "0" * 64,
        "qualified_on": date(2026, 10, 8).isoformat(),
    }


def local_profile(
    path: Path,
    *,
    cost_policy: str = "zero-hosted",
    context: int = 200_000,
    max_output: int = 4_096,
    reference: str | None = None,
) -> Path:
    """Write a non-hosted profile for the toy model (fixture data, not a qualification)."""
    profile: dict[str, Any] = {
        "version": 1,
        "kind": "non-hosted-responses",
        "requested_model": TOY_MODEL,
        "protocol": "counted-responses",
        "capabilities": {
            "context_window_tokens": context,
            "max_output_tokens": max_output,
            "cached_input_subset": True,
            "reasoning_output_subset": True,
        },
        "cost_policy": cost_policy,
        "provenance": provenance(),
        "operator_declaration": {
            "genuinely_non_hosted": True,
            "attested_by": "offline test fixture",
            "statement": "loopback toy server inside the test process",
        },
    }
    if reference is not None:
        profile["reference_rate_model"] = reference
    path.write_text(json.dumps(profile), encoding="utf-8")
    return path


def paid_profile(path: Path, *, fee_usd: str, max_request_bytes: int | None = None) -> Path:
    """Write a paid-kind profile with a SYNTHETIC fee: exercises the loader, never a real price."""
    profile = {
        "version": 1,
        "kind": "openai-standard-responses",
        "provider": "openai",
        "requested_model": "gpt-5.4",
        "response_models": [REFERENCE_MODEL],
        "service_tier": "default",
        "operations": {
            "responses.input_tokens": {
                "fee_upper_bound_usd": fee_usd,
                "max_request_bytes": max_request_bytes,
            },
            "responses.create": {"rates": "pinned-sdk-price-data"},
        },
        "capabilities": {
            "max_input_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "cached_input_subset": True,
            "reasoning_output_subset": True,
        },
        "provenance": provenance(),
        "operator_attestation": {
            "attested_by": "offline test fixture",
            "statement": "synthetic fee for loader/accounting tests; not a supplier price",
        },
    }
    path.write_text(json.dumps(profile), encoding="utf-8")
    return path


def write_config(path: Path, *, profile: Path, base_url: str, **limits: Any) -> Path:
    """Write a complete apprentice.toml for the local toy route with `limits` overrides."""
    values: dict[str, Any] = {
        "monthly_token_ceiling": 2_000_000,
        "monthly_cost_ceiling_usd": "50.0",
        "max_tokens_per_cycle": 100_000,
        "max_cost_per_cycle_usd": "5.0",
        "max_tokens_per_stage": 20_000,
        "max_tokens_per_agent_call": 20_000,
        "implementation_budget_pct": 40,
        "tool_agent_budget_pct": 15,
        "max_implementation_retries": 3,
        "max_prs_per_day": 2,
        "max_prs_per_week": 5,
        "max_concurrent_items": 1,
        "cooldown_hours": 0,
        "max_files_per_pr": 10,
        "max_lines_per_pr": 2000,
        "failure_threshold": 3,
        "half_open_probe_after_minutes": 60,
        "max_open_cycles_before_manual_reset": 3,
        "backend": "local",
        "model": f"openai/{TOY_MODEL}",
        "log_path": str(path.parent / "logs"),
    }
    values.update(limits)
    path.write_text(
        f"""[budget.global]
monthly_token_ceiling = {values["monthly_token_ceiling"]}
monthly_cost_ceiling_usd = {values["monthly_cost_ceiling_usd"]}

[budget.cycle]
max_tokens_per_cycle = {values["max_tokens_per_cycle"]}
max_cost_per_cycle_usd = {values["max_cost_per_cycle_usd"]}

[budget.stage]
max_tokens_per_stage = {values["max_tokens_per_stage"]}

[budget.agent]
max_tokens_per_agent_call = {values["max_tokens_per_agent_call"]}
implementation_budget_pct = {values["implementation_budget_pct"]}
tool_agent_budget_pct = {values["tool_agent_budget_pct"]}

[agents]
max_implementation_retries = {values["max_implementation_retries"]}

[rate_limits]
max_prs_per_day = {values["max_prs_per_day"]}
max_prs_per_week = {values["max_prs_per_week"]}
max_concurrent_items = {values["max_concurrent_items"]}
cooldown_hours = {values["cooldown_hours"]}
max_files_per_pr = {values["max_files_per_pr"]}
max_lines_per_pr = {values["max_lines_per_pr"]}

[circuit_breaker]
failure_threshold = {values["failure_threshold"]}
half_open_probe_after_minutes = {values["half_open_probe_after_minutes"]}
max_open_cycles_before_manual_reset = {values["max_open_cycles_before_manual_reset"]}

[provider]
backend = "{values["backend"]}"
model = "{values["model"]}"
local_api_base = "{base_url}"
accounting_profile_path = "{profile}"

[observability]
log_level = "WARNING"
log_format = "json"
log_path = "{values["log_path"]}"
metrics_enabled = true

[templates]
version = "1.0.0"
base_path = "config/templates"
""",
        encoding="utf-8",
    )
    return path
