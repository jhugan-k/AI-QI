"""Entrypoint for the API server.

On Windows the asyncio event-loop policy MUST be set before uvicorn creates
its loop. Setting it inside main.py is too late: when uvicorn loads the app
from an import string, it does so inside the already-running ProactorEventLoop.
So we set the policy here, before importing/starting uvicorn.

Run:  python -m services.api.run
"""

from __future__ import annotations

import asyncio
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn  # noqa: E402 — must come after the policy is set

from services.api.main import app  # noqa: E402

if __name__ == "__main__":
    # Bind 0.0.0.0 and honour $PORT so the same entrypoint works locally and on
    # a PaaS host (Render/Railway/etc.) that injects the port to listen on.
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
