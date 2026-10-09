"""Generation price authority: one hashed snapshot of the pinned LiteLLM price data.

Rates come only from the price-data resource shipped inside the pinned
`litellm` distribution, verified by SHA-256 before use. `litellm.model_cost`
(which can be refreshed from the network) is never consulted, and there is no
fallback table: a model without complete positive rates has no price.

Amounts are exact. Rates are Decimal nanodollars per token; quotes round up
to whole nanodollars so a reservation never undershoots the policy quote.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from importlib import metadata, resources

PINNED_SDKS: dict[str, str] = {"google-adk": "1.28.1", "litellm": "1.83.3", "openai": "2.30.0"}
PRICE_DATA_RESOURCE = "model_prices_and_context_window_backup.json"
PRICE_DATA_SHA256 = "7aacdb30a3021f3036a6d36d6a068fcfdc13c4a4a516ebc5afb272eb0012eb8f"

NANODOLLARS_PER_USD = Decimal(1_000_000_000)
_TIER_KEY = re.compile(r"input_cost_per_token_above_(\d+)k_tokens")


class PriceAuthorityError(Exception):
    """The installed SDKs or price data are not the pinned, hashed authority."""


class UnpricedModelError(Exception):
    """The price authority has no complete positive rates for a model."""


def usd_to_nanodollars(usd: Decimal) -> int:
    """Convert an exact USD amount to whole nanodollars, rejecting fractions."""
    nanos = usd * NANODOLLARS_PER_USD
    if nanos != nanos.to_integral_value():
        raise ValueError(f"{usd} USD is not a whole number of nanodollars")
    return int(nanos)


def nanodollars_to_usd(nanos: int) -> str:
    """Render whole nanodollars as an exact USD decimal string."""
    return format(Decimal(nanos) / NANODOLLARS_PER_USD, "f")


def _ceil(amount: Decimal) -> int:
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class TokenRates:
    """Nanodollars per token for one pricing tier."""

    input: Decimal
    cached_input: Decimal
    output: Decimal


@dataclass(frozen=True)
class ModelPrice:
    """Standard-tier rates and SDK capability maxima of one priced model.

    `long` applies to the whole request once its input exceeds
    `long_threshold` tokens (the documented tier switch); models without
    such rates have one tier.
    """

    model: str
    short: TokenRates
    long: TokenRates | None
    long_threshold: int | None
    max_input_tokens: int
    max_output_tokens: int

    def rates_for(self, input_tokens: int) -> TokenRates:
        if (
            self.long is not None
            and self.long_threshold is not None
            and input_tokens > self.long_threshold
        ):
            return self.long
        return self.short

    def worst_case_nanodollars(self, input_tokens: int, output_tokens: int) -> int:
        """Quote all input as uncached plus `output_tokens`, at the input's tier."""
        rates = self.rates_for(input_tokens)
        return _ceil(input_tokens * rates.input + output_tokens * rates.output)

    def charge_nanodollars(self, input_tokens: int, cached_tokens: int, output_tokens: int) -> int:
        """Quote reported usage: cached input is a subset of input, reasoning of output."""
        rates = self.rates_for(input_tokens)
        uncached = input_tokens - cached_tokens
        return _ceil(
            uncached * rates.input
            + cached_tokens * rates.cached_input
            + output_tokens * rates.output
        )

    def comparable(self) -> tuple[TokenRates, TokenRates | None, int | None]:
        return (self.short, self.long, self.long_threshold)


def _rate(record: dict[str, object], key: str, model: str) -> Decimal:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnpricedModelError(f"model {model!r} has no {key} in the pinned price data")
    rate = Decimal(repr(value)) * NANODOLLARS_PER_USD
    if not rate.is_finite() or rate <= 0:
        raise UnpricedModelError(f"model {model!r} has a non-positive {key}")
    return rate


def _limit(record: dict[str, object], key: str, model: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise UnpricedModelError(f"model {model!r} has no positive {key} in the pinned price data")
    return value


@dataclass(frozen=True)
class PriceSnapshot:
    """The verified pinned price data, keyed by exact model ID."""

    sha256: str
    sdk_versions: dict[str, str]
    records: dict[str, dict[str, object]]

    def model(self, model: str) -> ModelPrice:
        """Return the standard-tier price of `model`.

        Raises:
            UnpricedModelError: If the model is absent or any applicable rate
                or capability maximum is missing or not positive.
        """
        record = self.records.get(model)
        if not isinstance(record, dict):
            raise UnpricedModelError(f"model {model!r} is not in the pinned price data")
        short = TokenRates(
            input=_rate(record, "input_cost_per_token", model),
            cached_input=_rate(record, "cache_read_input_token_cost", model),
            output=_rate(record, "output_cost_per_token", model),
        )
        long: TokenRates | None = None
        threshold: int | None = None
        tiers = [m for key in record if (m := _TIER_KEY.fullmatch(key))]
        if len(tiers) > 1:
            raise UnpricedModelError(f"model {model!r} has more than one input tier switch")
        if tiers:
            suffix = f"above_{tiers[0].group(1)}k_tokens"
            threshold = int(tiers[0].group(1)) * 1000
            long = TokenRates(
                input=_rate(record, f"input_cost_per_token_{suffix}", model),
                cached_input=_rate(record, f"cache_read_input_token_cost_{suffix}", model),
                output=_rate(record, f"output_cost_per_token_{suffix}", model),
            )
        return ModelPrice(
            model=model,
            short=short,
            long=long,
            long_threshold=threshold,
            max_input_tokens=_limit(record, "max_input_tokens", model),
            max_output_tokens=_limit(record, "max_output_tokens", model),
        )


def load_price_snapshot() -> PriceSnapshot:
    """Verify the installed SDK pins and price-data hash, and return the snapshot.

    Raises:
        PriceAuthorityError: If an installed SDK version differs from its pin
            or the price-data bytes do not match the pinned SHA-256.
    """
    installed: dict[str, str] = {}
    for name, pinned in PINNED_SDKS.items():
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise PriceAuthorityError(f"pinned SDK {name}=={pinned} is not installed") from exc
        if version != pinned:
            raise PriceAuthorityError(f"installed {name} {version} is not the pinned {pinned}")
        installed[name] = version
    data = resources.files("litellm").joinpath(PRICE_DATA_RESOURCE).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != PRICE_DATA_SHA256:
        raise PriceAuthorityError(
            f"litellm price data {digest} does not match the pinned {PRICE_DATA_SHA256}"
        )
    records = json.loads(data)
    if not isinstance(records, dict):
        raise PriceAuthorityError("litellm price data is not a JSON object")
    return PriceSnapshot(sha256=digest, sdk_versions=installed, records=records)
