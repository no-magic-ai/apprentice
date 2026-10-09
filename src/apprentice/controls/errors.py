"""Control outcomes: neutral denials versus a broken authority."""

from __future__ import annotations


class ControlDeniedError(Exception):
    """A configured control refused admission before any effect.

    `control` names the limiting key or rule (for example
    `budget.stage.max_tokens_per_stage`). Denials are neutral outcomes: they
    are not infrastructure failures.
    """

    def __init__(self, control: str, message: str) -> None:
        super().__init__(f"{control}: {message}")
        self.control = control


class AuthorityError(Exception):
    """The durable control authority is missing, partial, corrupt or inconsistent.

    Nothing is admitted while this holds; the state is never reinitialized.
    """
