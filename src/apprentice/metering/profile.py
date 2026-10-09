"""Accounting profiles: the operator's structured capability and fee declaration.

`provider.accounting_profile_path` names one version-1 JSON profile for the
configured route. Software checks its structure only: known kind, exact
model/operation/tier, finite fee bounds representable in nanodollars,
provenance digest/source/date and the operator's attestation. Whether the
declared fees and capabilities are genuine is the operator's obligation; a
valid profile is not a certification.

Two kinds exist:

- `openai-standard-responses` (paid): the standard-tier OpenAI Responses
  route at exactly `OPENAI_ENDPOINT`. Generation rates come from the pinned
  price snapshot; the profile supplies the counting endpoint's fee upper
  bound and may only narrow the SDK capability maxima.
- `non-hosted-responses` (local): a genuinely non-hosted server implementing
  the counted Responses protocol for the exact requested model, with its own
  context/output maxima. `cost_policy` is `zero-hosted` (actual hosted cost
  zero) or `sdk-reference-capacity` (budgets are charged at a separate
  reference model's pinned rates as modelled capacity, never as spend).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING

from apprentice.metering.pricing import UnpricedModelError, usd_to_nanodollars

if TYPE_CHECKING:
    from apprentice.metering.pricing import ModelPrice, PriceSnapshot

PROFILE_VERSION = 1
KIND_PAID = "openai-standard-responses"
KIND_LOCAL = "non-hosted-responses"
OPENAI_ENDPOINT = "https://api.openai.com/v1"
COUNT_OPERATION = "responses.input_tokens"
GENERATE_OPERATION = "responses.create"
STANDARD_TIER = "default"

POLICY_QUOTE = "qualified-price-quote"
POLICY_ZERO_HOSTED = "zero-hosted"
POLICY_REFERENCE = "sdk-reference-capacity"

_SHA256 = re.compile(r"[0-9a-f]{64}")


class ProfileError(Exception):
    """The accounting profile is missing, malformed or does not apply to the route."""


@dataclass(frozen=True)
class Provenance:
    source: str
    sha256: str
    qualified_on: str
    attested_by: str
    statement: str


@dataclass(frozen=True)
class CounterFee:
    """Upper bound of one counting request's fee and the request envelope it covers."""

    nanodollars: int
    max_request_bytes: int | None


@dataclass(frozen=True)
class AccountingProfile:
    """A validated profile bound to its exact file bytes (`sha256`).

    `price` quotes generation for budgets: the paid model's own rates, the
    reference model's rates for `sdk-reference-capacity`, or None for
    `zero-hosted`. It never supplies model identity or capabilities.
    """

    path: str
    sha256: str
    kind: str
    cost_policy: str
    requested_model: str
    response_models: frozenset[str]
    max_input_tokens: int
    max_output_tokens: int
    cached_input_subset: bool
    reasoning_output_subset: bool
    counter_fee: CounterFee
    price: ModelPrice | None
    reference_rate_model: str | None
    provenance: Provenance

    @property
    def hosted(self) -> bool:
        return self.kind == KIND_PAID

    def describe(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "kind": self.kind,
            "cost_policy": self.cost_policy,
            "requested_model": self.requested_model,
            "response_models": sorted(self.response_models),
            "reference_rate_model": self.reference_rate_model,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "provenance": {
                "source": self.provenance.source,
                "sha256": self.provenance.sha256,
                "qualified_on": self.provenance.qualified_on,
                "attested_by": self.provenance.attested_by,
            },
        }


def _object(
    value: object, where: str, keys: set[str], optional: frozenset[str] = frozenset()
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProfileError(f"{where} must be an object")
    missing = keys - value.keys()
    unknown = value.keys() - keys - optional
    if missing:
        raise ProfileError(f"{where} is missing {sorted(missing)}")
    if unknown:
        raise ProfileError(f"{where} has unsupported fields {sorted(unknown)}")
    return value


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{where} must be a non-empty string")
    return value


def _positive_int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProfileError(f"{where} must be a positive integer")
    return value


def _flag(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise ProfileError(f"{where} must be a boolean")
    return value


def _fee(value: object, where: str) -> int:
    if not isinstance(value, str):
        raise ProfileError(f"{where} must be a decimal string")
    try:
        usd = Decimal(value)
    except InvalidOperation as exc:
        raise ProfileError(f"{where} is not a decimal number") from exc
    if not usd.is_finite() or usd < 0:
        raise ProfileError(f"{where} must be finite and nonnegative")
    try:
        return usd_to_nanodollars(usd)
    except ValueError as exc:
        raise ProfileError(f"{where}: {exc}") from exc


def _provenance(raw: dict[str, object], attestation_key: str) -> Provenance:
    prov = _object(raw.get("provenance"), "provenance", {"source", "sha256", "qualified_on"})
    digest = _text(prov["sha256"], "provenance.sha256")
    if not _SHA256.fullmatch(digest):
        raise ProfileError("provenance.sha256 must be 64 lowercase hex digits")
    qualified = _text(prov["qualified_on"], "provenance.qualified_on")
    try:
        day = date.fromisoformat(qualified)
    except ValueError as exc:
        raise ProfileError("provenance.qualified_on must be an ISO date") from exc
    if day > datetime.now(tz=UTC).date():
        raise ProfileError("provenance.qualified_on is in the future")
    attestation = raw.get(attestation_key)
    if attestation_key == "operator_declaration":
        declared = _object(
            attestation, attestation_key, {"genuinely_non_hosted", "attested_by", "statement"}
        )
        if declared["genuinely_non_hosted"] is not True:
            raise ProfileError("operator_declaration.genuinely_non_hosted must be true")
    else:
        declared = _object(attestation, attestation_key, {"attested_by", "statement"})
    return Provenance(
        source=_text(prov["source"], "provenance.source"),
        sha256=digest,
        qualified_on=qualified,
        attested_by=_text(declared["attested_by"], f"{attestation_key}.attested_by"),
        statement=_text(declared["statement"], f"{attestation_key}.statement"),
    )


def _priced(prices: PriceSnapshot, model: str) -> ModelPrice:
    try:
        return prices.model(model)
    except UnpricedModelError as exc:
        raise ProfileError(str(exc)) from exc


def _paid(
    raw: dict[str, object], path: str, digest: str, prices: PriceSnapshot
) -> AccountingProfile:
    _object(
        raw,
        "profile",
        {
            "version",
            "kind",
            "provider",
            "requested_model",
            "response_models",
            "service_tier",
            "operations",
            "capabilities",
            "provenance",
            "operator_attestation",
        },
    )
    if raw["provider"] != "openai":
        raise ProfileError("a paid profile must name provider 'openai'")
    if raw["service_tier"] != STANDARD_TIER:
        raise ProfileError(f"only the standard service tier {STANDARD_TIER!r} is supported")
    requested = _text(raw["requested_model"], "requested_model")
    echoes = raw["response_models"]
    if not isinstance(echoes, list) or not echoes:
        raise ProfileError("response_models must be a non-empty list")
    response_models = frozenset(_text(m, "response_models[]") for m in echoes) | {requested}
    price = _priced(prices, requested)
    for model in response_models:
        if _priced(prices, model).comparable() != price.comparable():
            raise ProfileError(f"response model {model!r} is not priced like {requested!r}")
    operations = _object(raw["operations"], "operations", {COUNT_OPERATION, GENERATE_OPERATION})
    count = _object(
        operations[COUNT_OPERATION], COUNT_OPERATION, {"fee_upper_bound_usd", "max_request_bytes"}
    )
    envelope = count["max_request_bytes"]
    fee = CounterFee(
        nanodollars=_fee(count["fee_upper_bound_usd"], f"{COUNT_OPERATION}.fee_upper_bound_usd"),
        max_request_bytes=None
        if envelope is None
        else _positive_int(envelope, f"{COUNT_OPERATION}.max_request_bytes"),
    )
    generate = _object(operations[GENERATE_OPERATION], GENERATE_OPERATION, {"rates"})
    if generate["rates"] != "pinned-sdk-price-data":
        raise ProfileError(f"{GENERATE_OPERATION}.rates must be 'pinned-sdk-price-data'")
    caps = _object(
        raw["capabilities"],
        "capabilities",
        {"max_input_tokens", "max_output_tokens", "cached_input_subset", "reasoning_output_subset"},
    )
    max_input = _positive_int(caps["max_input_tokens"], "capabilities.max_input_tokens")
    max_output = _positive_int(caps["max_output_tokens"], "capabilities.max_output_tokens")
    if max_input > price.max_input_tokens or max_output > price.max_output_tokens:
        raise ProfileError("profile capabilities may only narrow the pinned SDK model maxima")
    cached = _flag(caps["cached_input_subset"], "capabilities.cached_input_subset")
    reasoning = _flag(caps["reasoning_output_subset"], "capabilities.reasoning_output_subset")
    if not (cached and reasoning):
        raise ProfileError("the OpenAI Responses route reports cached and reasoning subsets")
    return AccountingProfile(
        path=path,
        sha256=digest,
        kind=KIND_PAID,
        cost_policy=POLICY_QUOTE,
        requested_model=requested,
        response_models=response_models,
        max_input_tokens=max_input,
        max_output_tokens=max_output,
        cached_input_subset=cached,
        reasoning_output_subset=reasoning,
        counter_fee=fee,
        price=price,
        reference_rate_model=None,
        provenance=_provenance(raw, "operator_attestation"),
    )


def _local(
    raw: dict[str, object], path: str, digest: str, prices: PriceSnapshot
) -> AccountingProfile:
    _object(
        raw,
        "profile",
        {
            "version",
            "kind",
            "requested_model",
            "protocol",
            "capabilities",
            "cost_policy",
            "provenance",
            "operator_declaration",
        },
        frozenset({"reference_rate_model"}),
    )
    if raw["protocol"] != "counted-responses":
        raise ProfileError("a non-hosted profile must declare protocol 'counted-responses'")
    requested = _text(raw["requested_model"], "requested_model")
    caps = _object(
        raw["capabilities"],
        "capabilities",
        {
            "context_window_tokens",
            "max_output_tokens",
            "cached_input_subset",
            "reasoning_output_subset",
        },
    )
    context = _positive_int(caps["context_window_tokens"], "capabilities.context_window_tokens")
    max_output = _positive_int(caps["max_output_tokens"], "capabilities.max_output_tokens")
    policy = raw["cost_policy"]
    reference = raw.get("reference_rate_model")
    price: ModelPrice | None = None
    if policy == POLICY_ZERO_HOSTED:
        if reference is not None:
            raise ProfileError("reference_rate_model applies only to sdk-reference-capacity")
    elif policy == POLICY_REFERENCE:
        reference = _text(reference, "reference_rate_model")
        price = _priced(prices, reference)
    else:
        raise ProfileError(f"cost_policy must be {POLICY_ZERO_HOSTED!r} or {POLICY_REFERENCE!r}")
    return AccountingProfile(
        path=path,
        sha256=digest,
        kind=KIND_LOCAL,
        cost_policy=str(policy),
        requested_model=requested,
        response_models=frozenset({requested}),
        max_input_tokens=context,
        max_output_tokens=max_output,
        cached_input_subset=_flag(caps["cached_input_subset"], "capabilities.cached_input_subset"),
        reasoning_output_subset=_flag(
            caps["reasoning_output_subset"], "capabilities.reasoning_output_subset"
        ),
        counter_fee=CounterFee(nanodollars=0, max_request_bytes=None),
        price=price,
        reference_rate_model=reference if isinstance(reference, str) else None,
        provenance=_provenance(raw, "operator_declaration"),
    )


def load_profile(path: str, prices: PriceSnapshot) -> AccountingProfile:
    """Read and validate the profile at `path`.

    Raises:
        ProfileError: If no profile is configured, it cannot be read, or its
            structure, kind, rates or provenance are invalid.
    """
    if not path:
        raise ProfileError(
            "no accounting profile is configured: set [provider].accounting_profile_path to an "
            "operator-qualified version-1 profile for this route"
        )
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise ProfileError(f"cannot read accounting profile {path}: {exc}") from exc
    try:
        raw = json.loads(data, parse_float=Decimal)
    except ValueError as exc:
        raise ProfileError(f"accounting profile {path} is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"accounting profile {path} must be a JSON object")
    if raw.get("version") != PROFILE_VERSION or isinstance(raw.get("version"), bool):
        raise ProfileError(f"accounting profile {path} must have version {PROFILE_VERSION}")
    digest = hashlib.sha256(data).hexdigest()
    kind = raw.get("kind")
    try:
        if kind == KIND_PAID:
            return _paid(raw, path, digest, prices)
        if kind == KIND_LOCAL:
            return _local(raw, path, digest, prices)
    except ProfileError as exc:
        raise ProfileError(f"accounting profile {path}: {exc}") from exc
    raise ProfileError(f"accounting profile {path} has unsupported kind {kind!r}")
