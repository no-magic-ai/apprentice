"""Route resolution: only a qualified backend, model and accounting profile yield a route."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from apprentice.core.config import load_config
from apprentice.metering.profile import ProfileError
from apprentice.providers.factory import RouteError, resolve_route
from tests.responses_fixture import local_profile, paid_profile, write_config

if TYPE_CHECKING:
    from pathlib import Path


class TestPaidRouteResolution:
    def test_missing_profile_is_rejected_before_any_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "offline-test-not-a-key")
        config = load_config(
            write_config(
                tmp_path / "apprentice.toml",
                profile=tmp_path / "absent.json",
                base_url="",
                backend="openai",
                model="openai/gpt-5.4",
            )
        )

        with pytest.raises(ProfileError, match="cannot read accounting profile"):
            resolve_route(config.provider)

    def test_environment_base_url_redirect_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "offline-test-not-a-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
        profile = paid_profile(tmp_path / "paid.json", fee_usd="0")
        config = load_config(
            write_config(
                tmp_path / "apprentice.toml",
                profile=profile,
                base_url="",
                backend="openai",
                model="openai/gpt-5.4",
            )
        )

        with pytest.raises(RouteError, match="OPENAI_BASE_URL"):
            resolve_route(config.provider)

    def test_paid_route_binds_the_exact_openai_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "offline-test-not-a-key")
        config = load_config(
            write_config(
                tmp_path / "apprentice.toml",
                profile=paid_profile(tmp_path / "paid.json", fee_usd="0"),
                base_url="",
                backend="openai",
                model="openai/gpt-5.4",
            )
        )

        route = resolve_route(config.provider)

        assert route.endpoint == "https://api.openai.com/v1"
        assert route.transport is None

    def test_paid_route_rejects_a_local_api_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "offline-test-not-a-key")
        config = load_config(
            write_config(
                tmp_path / "apprentice.toml",
                profile=paid_profile(tmp_path / "paid.json", fee_usd="0"),
                base_url="http://127.0.0.1:9/v1",
                backend="openai",
                model="openai/gpt-5.4",
            )
        )

        with pytest.raises(RouteError, match="local_api_base must be empty"):
            resolve_route(config.provider)

    def test_paid_profile_cannot_widen_the_pinned_model_maxima(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "offline-test-not-a-key")
        path = paid_profile(tmp_path / "paid.json", fee_usd="0")
        data = json.loads(path.read_text())
        data["capabilities"]["max_output_tokens"] = 128_001
        path.write_text(json.dumps(data))
        config = load_config(
            write_config(
                tmp_path / "apprentice.toml",
                profile=path,
                base_url="",
                backend="openai",
                model="openai/gpt-5.4",
            )
        )

        with pytest.raises(ProfileError, match="only narrow"):
            resolve_route(config.provider)

    @pytest.mark.parametrize("fee", ["-0.1", "1e-10", "NaN", "free"])
    def test_paid_counter_fee_must_be_finite_nonnegative_nanodollars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fee: str
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "offline-test-not-a-key")
        config = load_config(
            write_config(
                tmp_path / "apprentice.toml",
                profile=paid_profile(tmp_path / "paid.json", fee_usd=fee),
                base_url="",
                backend="openai",
                model="openai/gpt-5.4",
            )
        )

        with pytest.raises(ProfileError, match="fee_upper_bound_usd"):
            resolve_route(config.provider)


class TestRouteQualification:
    @pytest.mark.parametrize(
        "base", ["http://10.0.0.5:8080/v1", "http://localhost:8080/v1", "http://u:p@127.0.0.1/v1"]
    )
    def test_local_route_requires_a_plain_loopback_endpoint(
        self, tmp_path: Path, base: str
    ) -> None:
        config = load_config(
            write_config(
                tmp_path / "a.toml", profile=local_profile(tmp_path / "p.json"), base_url=base
            )
        )

        with pytest.raises(RouteError):
            resolve_route(config.provider)

    def test_model_override_must_be_the_profiled_model(self, tmp_path: Path) -> None:
        config = load_config(
            write_config(
                tmp_path / "a.toml",
                profile=local_profile(tmp_path / "p.json"),
                base_url="http://127.0.0.1:9/v1",
            )
        )

        with pytest.raises(RouteError, match="qualifies model"):
            resolve_route(config.provider, model="openai/gpt-5.4")

    def test_reference_policy_without_a_priced_reference_model_is_rejected(
        self, tmp_path: Path
    ) -> None:
        profile = local_profile(
            tmp_path / "p.json",
            cost_policy="sdk-reference-capacity",
            reference="not-a-priced-model",
        )
        config = load_config(
            write_config(tmp_path / "a.toml", profile=profile, base_url="http://127.0.0.1:9/v1")
        )

        with pytest.raises(ProfileError, match="not in the pinned price data"):
            resolve_route(config.provider)

    def test_unknown_profile_fields_are_rejected(self, tmp_path: Path) -> None:
        path = local_profile(tmp_path / "p.json")
        data = json.loads(path.read_text())
        data["capabilities"]["free_counting"] = True
        path.write_text(json.dumps(data))
        config = load_config(
            write_config(tmp_path / "a.toml", profile=path, base_url="http://127.0.0.1:9/v1")
        )

        with pytest.raises(ProfileError, match="unsupported fields"):
            resolve_route(config.provider)

    @pytest.mark.parametrize("backend", ["anthropic", "gemini", "ollama", "claude_cli"])
    def test_removed_backend_override_is_rejected_with_its_reason(
        self, tmp_path: Path, backend: str
    ) -> None:
        config = load_config(
            write_config(
                tmp_path / "a.toml",
                profile=local_profile(tmp_path / "p.json"),
                base_url="http://127.0.0.1:9/v1",
            )
        )

        with pytest.raises(RouteError, match=f"backend '{backend}' was removed"):
            resolve_route(config.provider, backend=backend)
