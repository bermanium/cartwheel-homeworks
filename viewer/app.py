"""A local, read-only trace viewer for the Cartwheel support agent.

Reads traces from the Langfuse API configured in ``.env`` and serves a plain
HTML/CSS/JS front end. Read-only on purpose: no annotation controls, no
database, nothing writes back to Langfuse.

Run it:

    SSL_CERT_FILE=$HOME/.local/share/meta-ca-bundle.pem \
    .venv/bin/python -m uvicorn viewer.app:app --port 8030

then open http://localhost:8030. The certificate variable is needed on this
machine to reach Langfuse Cloud over TLS; against a local Langfuse it can be
omitted.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from analysis.helpers.langfuse_io import LangfuseNotConfigured
from observability.instrument import load_env
from viewer import read

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    load_env()  # LANGFUSE_* come from .env, same as the agent server
    yield


app = FastAPI(title="Cartwheel trace viewer", lifespan=lifespan)


def _guard(call):
    """Turn a Langfuse configuration or transport problem into a clean 502.

    The front end shows the message verbatim, so a misconfigured .env says so
    instead of rendering an empty list that looks like "no traces yet".
    """
    try:
        return call()
    except LangfuseNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        raise HTTPException(
            status_code=502, detail=f"{type(exc).__name__}: {exc}"
        ) from exc


@app.get("/api/traces")
def api_traces(limit: int = 50) -> list[dict[str, Any]]:
    return _guard(lambda: read.list_traces(limit=limit))


@app.get("/api/traces/{trace_id}")
def api_trace(trace_id: str) -> dict[str, Any]:
    return _guard(lambda: read.get_trace(trace_id))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
