"""Single-origin local demo API; shared-key gate is not enterprise RBAC."""
from functools import lru_cache
import logging
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from .graph_engine import AegisGraphEngine, Conflict, NotFound, tenant_name

app = FastAPI(title="AEGIS", version="0.1.0")
logger = logging.getLogger(__name__)


@lru_cache
def engine():
    return AegisGraphEngine()


def tenant(request: Request, x_tenant_id: str = Header(default="demo"),
           x_api_key: str = Header(default="")):
    key = os.getenv("AEGIS_API_KEY", "")
    if key:
        if not secrets.compare_digest(key, x_api_key):
            raise HTTPException(401, "Invalid API key")
    elif request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "Nonlocal access requires AEGIS_API_KEY")
    try:
        tenant_name(x_tenant_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return x_tenant_id


@app.exception_handler(NotFound)
async def not_found(request, exc):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(Conflict)
async def conflict(request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(RedisError)
async def database_error(request, exc):
    logger.exception("FalkorDB request failed")
    return JSONResponse(status_code=503, content={"detail": "FalkorDB query failed; inspect server logs. No fallback data was used."})


class InvestigationInput(BaseModel):
    incident_id: str = Field(min_length=1, max_length=100)
    user_query: str = Field(default="", max_length=4000)


class RetentionInput(BaseModel):
    threshold: float = Field(default=0.1, ge=0, le=1, allow_inf_nan=False)
    dry_run: bool = True


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "frontend" / "index.html")


@app.get("/api/health")
def health():
    return engine().health()


@app.post("/api/seed")
def seed(t=Depends(tenant)):
    return engine().initialize_tenant_graph(t)


@app.get("/api/graph")
def graph(t=Depends(tenant)):
    return engine().graph_snapshot(t)


@app.get("/api/incidents")
def incidents(t=Depends(tenant)):
    return engine().incidents(t)


@app.get("/api/algorithms")
def algorithms(t=Depends(tenant)):
    return engine().algorithms(t)


@app.post("/api/investigations")
def investigate(body: InvestigationInput, t=Depends(tenant)):
    return engine().investigate(t, body.incident_id, body.user_query)


@app.get("/api/investigations/{trace}")
def investigation(trace: str, t=Depends(tenant)):
    return engine().get_investigation(t, trace)


@app.post("/api/investigations/{trace}/resume")
def resume(trace: str, t=Depends(tenant)):
    return engine().resume(t, trace)


@app.post("/api/investigations/{trace}/approve")
def approve(trace: str, t=Depends(tenant)):
    return engine().approve(t, trace)


@app.post("/api/investigations/{trace}/simulate")
def simulate(trace: str, t=Depends(tenant)):
    return engine().simulate(t, trace)


@app.get("/api/memories")
def memories(t=Depends(tenant)):
    return engine().memories(t)


@app.post("/api/memories/retain")
def retain(body: RetentionInput, t=Depends(tenant)):
    return engine().retain(t, body.threshold, body.dry_run)


@app.get("/api/search")
def search(q: str = "", t=Depends(tenant)):
    if len(q) > 500:
        raise HTTPException(422, "Query is too long")
    return engine().search(t, q)
