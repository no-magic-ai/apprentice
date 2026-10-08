"""Model routes — resolve the configured or overridden route and bind it to a cycle.

Every factory model is a `LiteLlm` whose client meters each request against
the installation authority (`apprentice.metering.client`). A route is
resolved once, before any cycle is admitted or request sent: the backend must
be supported, the pinned SDKs and price data must verify, and the accounting
profile must apply to exactly this backend and model. CLI and library
overrides go through the same resolution; nothing changes the model or
provider automatically and nothing mutates the process environment.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from google.adk.models.lite_llm import LiteLlm

from apprentice.core.config import backend_problem
from apprentice.metering.client import MeteredResponsesClient
from apprentice.metering.pricing import load_price_snapshot
from apprentice.metering.profile import KIND_LOCAL, KIND_PAID, OPENAI_ENDPOINT, load_profile

if TYPE_CHECKING:
    import httpx
    from openai import AsyncOpenAI

    from apprentice.controls.authority import Cycle
    from apprentice.core.config import ProviderConfig
    from apprentice.metering.profile import AccountingProfile

_PROVIDER_PREFIX = "openai/"
_BASE_URL_VARIABLES = ("OPENAI_BASE_URL", "OPENAI_API_BASE")
# Non-hosted servers need no credential; the hosted key is never sent to them.
_NON_HOSTED_KEY = "apprentice-non-hosted"


class RouteError(Exception):
    """The configured or requested route cannot be used; nothing was sent."""


@dataclass(frozen=True)
class ModelRoute:
    """A qualified model route: endpoint, credential and accounting profile.

    `transport` replaces the HTTP transport (an internal injection point for
    loopback test servers); production routes leave it None.
    """

    backend: str
    model_string: str
    endpoint: str
    profile: AccountingProfile
    price_sha256: str
    api_key: str = field(repr=False)
    transport: httpx.AsyncBaseTransport | None = None

    def openai_client(self, http: httpx.AsyncClient) -> AsyncOpenAI:
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=self.api_key, base_url=self.endpoint, max_retries=0, http_client=http
        )

    def model(self, cycle: Cycle, stage: str, role: str) -> LiteLlm:
        """Return the factory model of one role, metered against `cycle`."""
        return LiteLlm(
            model=self.model_string,
            llm_client=MeteredResponsesClient(self, cycle, stage, role),
        )

    def describe(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "model": self.model_string,
            "endpoint": self.endpoint,
            "price_data_sha256": self.price_sha256,
            "profile": self.profile.describe(),
        }


def _loopback_endpoint(base: str) -> str:
    parts = urlsplit(base)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise RouteError(f"[provider].local_api_base {base!r} must be an http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise RouteError(
            f"[provider].local_api_base {base!r} must not carry credentials, a query or a fragment"
        )
    try:
        loopback = ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise RouteError(
            f"[provider].local_api_base {base!r} must be a loopback IP address; "
            "a non-hosted route never leaves this machine"
        )
    return base.rstrip("/")


def _reject_base_overrides(endpoint: str) -> None:
    for name in _BASE_URL_VARIABLES:
        value = os.environ.get(name)
        if value and value.rstrip("/") != endpoint:
            raise RouteError(
                f"environment variable {name}={value!r} conflicts with the route endpoint "
                f"{endpoint}; unset it — apprentice never redirects a route through the "
                "environment"
            )


def resolve_route(
    provider: ProviderConfig,
    *,
    backend: str | None = None,
    model: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelRoute:
    """Resolve the configured route, with optional backend/model overrides.

    Raises:
        RouteError: If the backend is unsupported or misconfigured, or the
            accounting profile does not apply to the route.
        apprentice.metering.profile.ProfileError: If the profile is missing
            or invalid.
        apprentice.metering.pricing.PriceAuthorityError: If the installed
            SDKs or price data are not the pinned authority.
    """
    chosen_backend = backend or provider.backend
    problem = backend_problem(chosen_backend)
    if problem:
        raise RouteError(problem)
    model_string = model or provider.model
    prices = load_price_snapshot()
    profile = load_profile(provider.accounting_profile_path, prices)
    requested = model_string.removeprefix(_PROVIDER_PREFIX)
    if requested != profile.requested_model:
        raise RouteError(
            f"accounting profile {profile.path} qualifies model {profile.requested_model!r}, "
            f"not {model_string!r}"
        )
    if chosen_backend == "openai":
        if profile.kind != KIND_PAID:
            raise RouteError(
                f"backend 'openai' needs a {KIND_PAID!r} profile, not {profile.kind!r}"
            )
        if provider.local_api_base:
            raise RouteError(
                "[provider].local_api_base must be empty for backend 'openai'; the paid route "
                f"is always {OPENAI_ENDPOINT}"
            )
        endpoint = OPENAI_ENDPOINT
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RouteError("backend 'openai' requires the OPENAI_API_KEY environment variable")
    else:
        if profile.kind != KIND_LOCAL:
            raise RouteError(
                f"backend 'local' needs a {KIND_LOCAL!r} profile, not {profile.kind!r}"
            )
        endpoint = _loopback_endpoint(provider.local_api_base)
        api_key = _NON_HOSTED_KEY
    _reject_base_overrides(endpoint)
    return ModelRoute(
        backend=chosen_backend,
        model_string=model_string,
        endpoint=endpoint,
        profile=profile,
        price_sha256=prices.sha256,
        api_key=api_key,
        transport=transport,
    )
