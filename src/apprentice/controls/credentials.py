"""Separation of the publication capability from generation and validation.

Only `apprentice submit` holds a GitHub write credential, and only the one it
is given explicitly in `APPRENTICE_PUBLISH_TOKEN`: there is no fallback to
`gh` authentication, `GH_TOKEN`/`GITHUB_TOKEN`, git credential helpers or any
other ambient source. `PublisherCapability` validates that token locally,
before anything is admitted or claimed, and then only hands it to the fixed
publisher subprocesses (`git push`, `gh pr create`) through their own
environments; it never appears in configuration, a repr, argv, serialized
state, records or logs.

Every generation and validation entry point refuses to start while a known
publication credential is present in the process environment
(`require_generation_environment`), so a model, validator or generated
program never runs next to one. This is a presence check on the known
variable names, not a claim that arbitrary other secrets are stripped or that
processes of the same host user are isolated from each other.
"""

from __future__ import annotations

import base64
import os
import re
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Mapping

PUBLISH_TOKEN_VARIABLE = "APPRENTICE_PUBLISH_TOKEN"

# Environment variables that carry GitHub write credentials for git, gh or apprentice.
PUBLICATION_CREDENTIAL_VARIABLES = (
    PUBLISH_TOKEN_VARIABLE,
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    # The standard credential of GitHub's MCP server (github/github-mcp-server).
    "GITHUB_PERSONAL_ACCESS_TOKEN",
)

_TOKEN = re.compile(r"[A-Za-z0-9_]{20,255}")


class PublicationCredentialError(Exception):
    """A publication credential is missing, malformed, or present where it must not be."""


def require_generation_environment(environ: Mapping[str, str] | None = None) -> None:
    """Refuse generation or validation while a known publication credential is set.

    Raises:
        PublicationCredentialError: Naming (never showing) every credential variable present.
    """
    source = os.environ if environ is None else environ
    present = [name for name in PUBLICATION_CREDENTIAL_VARIABLES if source.get(name)]
    if present:
        raise PublicationCredentialError(
            f"publication credentials must not be present during generation or validation: "
            f"unset {', '.join(present)} (supply {PUBLISH_TOKEN_VARIABLE} only to "
            "`apprentice submit`)"
        )


class PublisherCapability:
    """The explicit, locally validated GitHub write credential of one submission.

    The token is kept private: it is not part of the repr, cannot be pickled or
    copied, and leaves this object only inside the environment built for one
    publisher subprocess.
    """

    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        if not _TOKEN.fullmatch(token):
            raise PublicationCredentialError(
                f"{PUBLISH_TOKEN_VARIABLE} is not a GitHub token (20-255 letters, digits or "
                "underscores)"
            )
        self._token = token

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> PublisherCapability:
        """Read the explicit publication token; nothing ambient is consulted.

        Raises:
            PublicationCredentialError: If it is unset, empty or malformed.
        """
        source = os.environ if environ is None else environ
        token = source.get(PUBLISH_TOKEN_VARIABLE, "")
        if not token:
            raise PublicationCredentialError(
                f"{PUBLISH_TOKEN_VARIABLE} is not set: `submit` publishes only with an explicitly "
                "supplied GitHub token (gh authentication, GH_TOKEN, GITHUB_TOKEN and git "
                "credential helpers are never used)"
            )
        return cls(token)

    def __repr__(self) -> str:
        return "PublisherCapability(<redacted>)"

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError("a publisher capability cannot be serialized or copied")

    def _basic(self) -> str:
        return base64.b64encode(f"x-access-token:{self._token}".encode()).decode("ascii")

    def git_http_header(self) -> str:
        """The HTTP authorization header git sends to the supported host."""
        return f"Authorization: Basic {self._basic()}"

    def gh_token(self) -> str:
        """The token for the `gh` subprocess environment (`GH_TOKEN`)."""
        return self._token

    def redact(self, text: str) -> str:
        """Replace the token, or its encoded form, wherever a tool echoed it."""
        return text.replace(self._basic(), "<redacted>").replace(self._token, "<redacted>")
