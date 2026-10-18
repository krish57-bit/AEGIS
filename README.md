# AegisGraph — graph-native incident investigation on FalkorDB

AegisGraph is a **deterministic, graph-backed incident investigation and simulated-remediation demo**
built for the *Graph Hacks: Context for AI Agents* hackathon (WeMakeDevs × FalkorDB). It connects
service topology, deployments, authors, owners, architecture decisions (ADRs), runbooks, playbooks and
agent memory in one FalkorDB property graph, and uses that graph to do the work: localizing candidate
causes, recalling similar history, coordinating a three-stage handoff, and gating a scoped simulation
behind explicit approval.

> **Honest scope.** Workflow stages are deterministic code, not LLM agents. Rankings are labeled
> heuristics, not causal proof. **Remediation is simulation only** — no production command is ever
> executed, and a simulated outcome is never recorded as a real recovery. All data is synthetic demo data.

## Hackathon tracks

| Track | What AegisGraph does with the graph |
| --- | --- |
| 01 – Agents that act on connected data | MCP tools + API over graph algorithms (`algo.pageRank`, `betweenness`, `HarmonicCentrality`, `WCC`, `labelPropagation`, `SPpaths`), bounded multi-hop traversals for localization and reverse-reachability blast radius, and a topology mutation (dependency edges → `ISOLATED_DEPENDENCY`) that changes later algorithm results. |
| 02 – Agent memory & coordination | `Investigation` → `AgentTask` (`LOCATE`→`SUGGEST`→`PROPOSE`) chain linked by `HANDOFF_TO`; each stage reads its predecessor's **persisted** output, so a restarted process resumes without redoing stages. Episodic memory (`Episode`, HNSW vector index), evidence protection, dry-run-first retention/archival, per-tenant graphs via `select_graph`. |
| 03 – Company brain | `Person`, `Team`, `Service`, `Commit`, `Decision`, `Document`, `Playbook`, `Incident` in one graph: who owns / authored what, which ADR governs a service, full-text runbook search, evidence paths back to sources. |

## How FalkorDB is used (and why removing it breaks the app)

* **Graph algorithms** as callable analysis tools over the live topology.
* **Variable-length Cypher** with relationship-status filters for candidate localization and blast radius.
* **Vector index** (`CREATE VECTOR INDEX … dimension 8, cosine`) + `db.idx.vector.queryNodes` for episode recall.
  The 8-d vectors are deterministic keyword features (a reproducible demo heuristic, **not** a semantic embedding model).
* **Full-text index** (`db.idx.fulltext.createNodeIndex`) for runbook search.
* **Graph-persisted workflow state** (`AgentTask`, `HANDOFF_TO`) instead of in-process variables.
* **Multigraph tenant isolation** with `select_graph("aegis_<tenant>")`; tenants are validated, allow-listed, never silently rewritten.
* Simulation commits topology change + outcome memory in **one** write query.

## Quick start

### Option A — embedded FalkorDB (no Docker)

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt falkordblite==0.10.0
GRAPH_MODE=embedded uvicorn backend.main:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

### Option B — Docker FalkorDB

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
docker compose up -d --wait          # FalkorDB on 127.0.0.1:6379
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

`GRAPH_MODE` defaults to `remote`; there is **no silent fallback** to embedded data if the database is down
(requests fail with HTTP 503). Pinned versions: `falkordb/falkordb:v4.14.9`, packages in `requirements.txt`.
Tested here with `falkordb==1.8.0` client and `falkordblite==0.10.0` on macOS/arm64, Python 3.14.

### Demo flow (≈3 minutes)

1. Open the dashboard → **Seed demo** → inspect the topology, algorithm capability cards, P0 incident `INC-2026-901`.
2. **Investigate**: LOCATE → SUGGEST → PROPOSE appear as persisted `AgentTask` stages. The proposal names
   `payment-processor`, commit `a8f921c`, its author/owning team, applicable `ADR-042`, blast radius, and evidence path.
3. Try **Simulate** first — it is rejected (409). Tick the approval box → **Approve** → **Simulate**.
   Only dependency edges into the approved target are isolated; observed telemetry is unchanged; incident stays `OPEN`.
4. **Memory**: preview retention (dry run), then archive; protected evidence is never archived.
5. **Company brain**: search "payment" for runbooks and ownership.

## Configuration

| Variable | Meaning |
| --- | --- |
| `GRAPH_MODE` | `remote` (default) or `embedded` |
| `FALKORDB_HOST` / `FALKORDB_PORT` / `FALKORDB_PASSWORD` | Remote connection |
| `FALKORDB_PATH` | Embedded data file (default `/tmp/aegisgraph.db`) |
| `AEGIS_API_KEY` | Shared key sent as `X-API-Key`; **required for non-loopback access** |
| `AEGIS_ALLOWED_TENANTS` | Comma-separated allow-list (default `demo`); tenant ids match `[a-z][a-z0-9_]{0,47}` |
| `AEGIS_MCP_TENANT` | Fixed tenant for the MCP server (must be allow-listed) |

A shared key + allow-list is **not** per-user RBAC; add real identity, TLS and rate limiting before any real deployment.

## HTTP API (`X-Tenant-ID: demo`)

| Method & path | Purpose |
| --- | --- |
| `GET /api/health` | DB reachability (public) |
| `POST /api/seed` | Idempotent tenant initialization |
| `GET /api/graph`, `/api/incidents`, `/api/algorithms`, `/api/memories` | Reads |
| `POST /api/investigations` `{incident_id, user_query}` | Run workflow; returns `PROPOSED` or `INSUFFICIENT_EVIDENCE` |
| `GET /api/investigations/{id}`, `POST …/{id}/resume` | Read / resume from persisted stages |
| `POST …/{id}/approve`, `POST …/{id}/simulate` | Explicit approval, then scoped simulation (409 otherwise) |
| `POST /api/memories/retain` `{threshold, dry_run=true}` | Preview (default) or archive |
| `GET /api/search?q=` | Full-text runbooks + ownership |

## MCP server (stdio)

```sh
GRAPH_MODE=embedded python -m backend.mcp_server
```

Tools: `read_graph`, `list_incidents`, `graph_algorithms`, `investigate_incident`, `read_investigation`,
`list_memories`, `preview_memory_retention` (always dry-run), `search_company_brain`. No tool accepts a tenant,
raw Cypher, approval, simulation, or destructive flag. Built on `mcp.server.mcpserver.MCPServer` (MCP SDK 2.x).

## Safety properties (each covered by tests)

* Unknown incident → 404; no supported candidate → `INSUFFICIENT_EVIDENCE`, no proposal, cannot be approved.
* Simulation requires prior approval, touches only approved targets, is idempotent, and leaves telemetry and incident status unchanged.
* Candidates must have a deployment inside the incident's 24 h window, matching environment, active dependency path, and an *accepted* ADR valid at incident time. Ownership (`OWNS`) and authorship (`AUTHORED`) are resolved separately.
* Unexecuted proposals never become success memories; the simulated outcome is labeled `SIMULATED_OUTCOME`.
* Retention is dry-run first, has a 24 h grace period, archives rather than deletes, and skips protected evidence.
* Interrupted investigations resume from the persisted `LOCATE` stage after a "restart".
* Optional algorithms report `available: false` instead of failing diagnosis or fabricating scores
  (`algo.HarmonicCentrality` is not registered in either `falkordb/falkordb:v4.14.9` or FalkorDBLite 0.10.0, so it is reported unavailable).

## Tests

```sh
pip install pytest
pytest                                  # fast tests (tenant rules, API gating, error mapping)
AEGIS_TEST_GRAPH=1 pytest               # + real embedded FalkorDB integration tests (needs falkordblite)
```

Last local run: 12 passed (6 fast, 6 against embedded FalkorDB). The full workflow (seed, algorithms,
investigate, approve, simulate, retention preview, search) was also run manually against the pinned
`falkordb/falkordb:v4.14.9` container, and an MCP stdio client handshake + tool call was exercised.
Those manual runs are not part of the automated suite.

## Known limitations

* Heuristic ranking (`risk_score` is seeded), not causal verification; heuristic scores are not calibrated confidence.
* Synthetic single-incident dataset; no real telemetry ingestion or live executor.
* Keyword-feature vectors, not learned embeddings; the cosine threshold is untuned.
* Task claiming is single-process (lock based); multi-worker leases are not implemented.
* The dashboard is a vanilla-JS/SVG page with no CDN dependencies.

## AI-assistance disclosure (hackathon Rule 11)

AI coding assistance (Claude via the Backboard IDE) was used to scaffold boilerplate, the dashboard, tests and
documentation. The graph data model, Cypher queries, agent-workflow design and safety constraints were reviewed
by the participant, and the Cypher was exercised against a real embedded FalkorDB instance. Research references
(CoALA, Zep/Graphiti, SGAgent) are conceptual inspiration only; no benchmark results are claimed.
