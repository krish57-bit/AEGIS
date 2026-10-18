"""Tenant-bound, simulation-only FastMCP adapter.

Run with ``python -m backend.mcp_server``. Configuration is read at process
startup; stdio clients must supply the environment explicitly.
"""

from __future__ import annotations

import math
import os
from threading import Lock
from typing import Any

from mcp.server.mcpserver import MCPServer


_TENANT = os.environ.get("AEGIS_MCP_TENANT", "demo").strip()
_ALLOWED_TENANTS = {
    tenant.strip()
    for tenant in os.environ.get("AEGIS_ALLOWED_TENANTS", "demo").split(",")
    if tenant.strip()
}
if not _TENANT or _TENANT not in _ALLOWED_TENANTS:
    raise ValueError("AEGIS_MCP_TENANT must belong to AEGIS_ALLOWED_TENANTS")

mcp = MCPServer(
    "Aegis",
    instructions=(
        "Tenant-bound deterministic incident investigation demo. Investigation "
        "can persist a trace; first use can initialize demo data. Retention is "
        "preview-only. No approval, execution, simulation, or deletion tools "
        "are exposed. Graph evidence is not proof of causality."
    ),
)

_engine: Any = None
_engine_lock = Lock()


def _get_engine() -> Any:
    """Connect and initialize only on first use, never during tool discovery."""
    global _engine
    with _engine_lock:
        if _engine is None:
            from backend.graph_engine import AegisGraphEngine

            engine = AegisGraphEngine()
            engine.initialize_tenant_graph(_TENANT)
            _engine = engine
    return _engine


def _required_text(value: str, field: str) -> str:
    if not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value


@mcp.tool()
def read_graph() -> Any:
    """Read the configured tenant's graph snapshot (may seed data on first use)."""
    return _get_engine().graph_snapshot(_TENANT)


@mcp.tool()
def list_incidents() -> Any:
    """List incidents in the configured tenant's graph."""
    return _get_engine().incidents(_TENANT)


@mcp.tool()
def graph_algorithms() -> Any:
    """Inspect graph algorithm results for the configured tenant."""
    return _get_engine().algorithms(_TENANT)


@mcp.tool()
def investigate_incident(incident_id: str, user_query: str = "") -> Any:
    """Run deterministic investigation and persist a trace, not a remediation."""
    incident_id = _required_text(incident_id, "incident_id")
    return _get_engine().investigate(_TENANT, incident_id, user_query=user_query)


@mcp.tool()
def read_investigation(trace_id: str) -> Any:
    """Read an existing investigation trace within the configured tenant."""
    trace_id = _required_text(trace_id, "trace_id")
    return _get_engine().get_investigation(_TENANT, trace_id)


@mcp.tool()
def list_memories() -> Any:
    """Read the configured tenant's retained investigation memories."""
    return _get_engine().memories(_TENANT)


@mcp.tool()
def preview_memory_retention(threshold: float = 0.1) -> Any:
    """Preview retention candidates without deleting anything; dry_run is fixed."""
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be finite and between 0 and 1")
    return _get_engine().retain(_TENANT, threshold=threshold, dry_run=True)


@mcp.tool()
def search_company_brain(query: str) -> Any:
    """Search documents and ownership in the configured tenant; not an arbitrary Cypher tool."""
    query = _required_text(query, "query")
    return _get_engine().search(_TENANT, query)


def main() -> None:
    """Serve MCP on stdio; reserve stdout for protocol messages."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
