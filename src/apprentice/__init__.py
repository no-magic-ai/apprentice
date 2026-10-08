"""apprentice — Agentic Algorithm Factory for no-magic."""

from __future__ import annotations

import os

# LiteLLM fetches its price map from the network at import unless told not
# to. Every quote uses the hashed pinned resource instead
# (apprentice.metering.pricing), so the remote refresh is disabled before any
# ADK or LiteLLM import made through this package.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
# In its default "DEV" mode LiteLLM loads a `.env` file found next to its
# installation into the process environment at import. Credentials and base
# URLs come only from the environment the operator set, so that implicit load
# is disabled the same way.
os.environ["LITELLM_MODE"] = "PRODUCTION"

__version__ = "0.4.0"
