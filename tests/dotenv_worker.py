"""Trusted helper process: does importing apprentice's SDKs load a `.env` file implicitly?

Usage: python dotenv_worker.py <absent|DEV>

Sets LiteLLM's mode the way an operator environment might (removed, or the
SDK default "DEV"), replaces `dotenv.load_dotenv` with a recorder before
anything imports LiteLLM — so no file is ever read — then imports apprentice
and the pinned price data (which imports LiteLLM). Prints the number of
implicit `load_dotenv` calls as JSON.
"""

from __future__ import annotations

import json
import os
import sys

import dotenv

if sys.argv[1] == "absent":
    os.environ.pop("LITELLM_MODE", None)
else:
    os.environ["LITELLM_MODE"] = sys.argv[1]
calls: list[object] = []


def _record(*args: object, **kwargs: object) -> bool:
    calls.append((args, kwargs))
    return False


dotenv.load_dotenv = _record  # type: ignore[assignment]

import apprentice  # noqa: E402,F401
from apprentice.metering.pricing import load_price_snapshot  # noqa: E402

load_price_snapshot()
print(json.dumps({"load_dotenv_calls": len(calls), "litellm_imported": "litellm" in sys.modules}))
