"""Errors of contained execution.

An infrastructure error means apprentice could not run, collect or tear down
generated code safely. It aborts the cycle: it is never turned into model
feedback (which would spend a retry on a fault the model cannot fix) and never
into a passing result. A generated program that fails inside its limits is not
an error here; it is an ordinary result.
"""

from __future__ import annotations


class SandboxInfrastructureError(Exception):
    """The backend, image, staging, output collection or teardown failed; the cycle aborts."""


class SandboxUnavailableError(SandboxInfrastructureError):
    """No supported execution backend is configured or reachable; nothing generated runs."""


class StagingError(SandboxInfrastructureError):
    """An input could not be staged: the trusted root or a read failed, or the file changed."""


class UnsafeInputError(StagingError):
    """A requested path names no stageable file: it leaves the root, or is a link,
    directory, special, hard-linked or oversized file. Nothing was read or run.

    The validator tools refuse such a request with an ordinary failed result;
    every other caller treats it as the staging failure it subclasses.
    """


class OutsideScopeError(UnsafeInputError):
    """A path names something outside the caller-authorized root."""
