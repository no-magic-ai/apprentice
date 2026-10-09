"""Trusted helper process for control-authority tests: one metered call in its own cycle.

Usage: python control_worker.py <store_dir> <config_path>

Opens the authority of <store_dir> (fresh footprint), admits a `suggest`
cycle and sends one discovery request through the configured route. The
parent test decides whether the call completes or the process is killed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from google.adk.models import LlmRequest
from google.genai import types

from apprentice.controls.authority import Authority
from apprentice.controls.footprint import Footprint
from apprentice.controls.policy import ControlPolicy
from apprentice.core.config import load_config
from apprentice.providers.factory import resolve_route


async def _call(model: object) -> None:
    request = LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text="Suggest one algorithm.")])],
        config=types.GenerateContentConfig(system_instruction="You are a curriculum designer."),
    )
    async for _ in model.generate_content_async(request):  # type: ignore[attr-defined]
        pass


def main() -> int:
    store_dir, config_path = Path(sys.argv[1]), Path(sys.argv[2])
    config = load_config(config_path)
    authority = Authority.open(store_dir, Footprint(existing=()))
    route = resolve_route(config.provider)
    with authority.begin_cycle("suggest", ControlPolicy.from_config(config)) as cycle:
        print("ADMITTED", flush=True)
        asyncio.run(_call(route.model(cycle, "discovery", "discovery")))
    print("FINISHED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
