"""Entrypoint for the ML microservice. Serves on :8001.

Run:  python -m services.ml.run
"""

from __future__ import annotations

import uvicorn

from services.ml.service import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
