"""Async Postgres connection pool for the API.

A pool keeps a small set of DB connections open and hands them out per request,
instead of paying the (expensive) TCP + auth handshake on every call. Opened on
app startup and closed on shutdown via the FastAPI lifespan.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from psycopg_pool import AsyncConnectionPool

load_dotenv()

# .strip() guards against a trailing newline/space sneaking in when the value is
# pasted into a dashboard env-var field — libpq rejects e.g. "channel_binding=require\n".
DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set — copy .env.example to .env")

# open=False: don't connect at import time; the lifespan handler opens it.
pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False)
