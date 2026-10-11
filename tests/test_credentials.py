"""The publication capability stays explicit, opaque and out of generation and validation."""

from __future__ import annotations

import copy
import json
import pickle
from typing import TYPE_CHECKING

import pytest

from apprentice.controls.credentials import (
    PUBLICATION_CREDENTIAL_VARIABLES,
    PublicationCredentialError,
    PublisherCapability,
)
from apprentice.core.config import ExecutionConfig, load_config
from apprentice.core.cycles import run_build
from apprentice.providers.factory import RouteError, resolve_route
from apprentice.sandbox.docker import open_sandbox
from apprentice.validators.tools import ValidatorTools
from tests.conftest import PUBLISH_TOKEN
from tests.responses_fixture import local_profile, write_config

if TYPE_CHECKING:
    from pathlib import Path


class TestPublisherCapability:
    @pytest.mark.parametrize(
        "environ",
        [
            {},
            {"APPRENTICE_PUBLISH_TOKEN": ""},
            {"GH_TOKEN": PUBLISH_TOKEN, "GITHUB_TOKEN": PUBLISH_TOKEN},
        ],
    )
    def test_only_the_explicit_variable_supplies_a_token(self, environ: dict[str, str]) -> None:
        with pytest.raises(PublicationCredentialError, match="APPRENTICE_PUBLISH_TOKEN is not set"):
            PublisherCapability.from_environment(environ)

    @pytest.mark.parametrize("token", ["short", "ghp_with space000000000000", "ghp_x\nInjected: 1"])
    def test_malformed_tokens_are_refused_without_echoing_them(self, token: str) -> None:
        with pytest.raises(PublicationCredentialError) as excinfo:
            PublisherCapability.from_environment({"APPRENTICE_PUBLISH_TOKEN": token})
        assert token not in str(excinfo.value)

    def test_the_token_never_leaves_through_repr_serialization_or_copies(self) -> None:
        capability = PublisherCapability(PUBLISH_TOKEN)

        assert PUBLISH_TOKEN not in repr(capability) + str(capability)
        for leak in (pickle.dumps, copy.copy, copy.deepcopy, json.dumps):
            with pytest.raises(TypeError):
                leak(capability)

    def test_tool_output_echoing_the_token_or_its_header_is_redacted(self) -> None:
        capability = PublisherCapability(PUBLISH_TOKEN)
        echoed = f"fatal: {capability.git_http_header()} rejected; token={PUBLISH_TOKEN}"

        redacted = capability.redact(echoed)

        assert PUBLISH_TOKEN not in redacted
        assert capability.git_http_header().split()[-1] not in redacted


class TestGenerationAndValidationRefuseCredentials:
    @pytest.mark.parametrize("name", PUBLICATION_CREDENTIAL_VARIABLES)
    def test_validation_entry_points_refuse_to_start(
        self, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(name, PUBLISH_TOKEN)

        with pytest.raises(PublicationCredentialError, match=name) as excinfo:
            open_sandbox(ExecutionConfig(docker_context="default", image="sha256:" + "0" * 64))
        assert PUBLISH_TOKEN not in str(excinfo.value)
        with pytest.raises(PublicationCredentialError, match=name):
            ValidatorTools(tmp_path)

    @pytest.mark.parametrize("name", PUBLICATION_CREDENTIAL_VARIABLES)
    def test_model_entry_points_refuse_before_any_route_cycle_or_request(
        self, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = load_config(
            write_config(
                tmp_path / "apprentice.toml",
                profile=local_profile(tmp_path / "profile.json"),
                base_url="http://127.0.0.1:9/v1",
            )
        )
        cached = resolve_route(config.provider)
        monkeypatch.setenv(name, PUBLISH_TOKEN)

        with pytest.raises(RouteError, match=name):
            resolve_route(config.provider)
        # A route resolved earlier still builds no model once the credential is present.
        with pytest.raises(PublicationCredentialError, match=name):
            cached.model(_Untouchable(), "implementation", "implementation")  # type: ignore[arg-type]
        with pytest.raises(PublicationCredentialError, match=name):
            run_build(
                store=_Untouchable(),  # type: ignore[arg-type]
                authority=_Untouchable(),  # type: ignore[arg-type]
                route=cached,
                policy=_Untouchable(),  # type: ignore[arg-type]
                request=("selection", 2, ""),
                kind="build",
                sandbox=_Untouchable(),  # type: ignore[arg-type]
            )


class _Untouchable:
    """A collaborator that must not be used: any attribute access fails the test."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"used {name!r} although a publication credential is present")
