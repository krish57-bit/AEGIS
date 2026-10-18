# WeMakeDevs submission pack (corrected, claim-safe)

Replace `<REPO_URL>`, `<VIDEO_URL>`, `<BLOG_URL>` before submitting. Deadline: Oct 18, 11:59 PM IST.

## Part 1

**1. Project title**
AegisGraph: Graph-Native Incident Investigation, Persisted Agent Handoffs and Company Brain on FalkorDB

**2. Description**
During production incidents, service topology, recent deployments, commit authors, owning teams, architecture
decisions (ADRs), runbooks and past incidents live in separate tools, and a stateless RAG assistant cannot
follow the multi-hop chain between them. AegisGraph puts all of it in one FalkorDB graph for SREs and platform
teams. A deterministic three-stage Locate → Suggest → Propose workflow traverses the dependency graph to rank
candidate causes (with explicit evidence paths and labeled heuristic scores), recalls similar episodes via a
vector index, persists every handoff as `AgentTask` nodes, and requires explicit approval before a *scoped,
simulation-only* mitigation that isolates dependency edges and re-runs graph algorithms on the changed
topology. Missing evidence yields `INSUFFICIENT_EVIDENCE` rather than a guess, and unexecuted proposals are
never stored as successful precedents. Data is synthetic; no production command is ever executed.

**3. Tracks**
All three: (1) Agents That Act on Connected Data, (2) Agent Memory and Coordination, (3) Company Brain.

**4. Repository**
`https://github.com/krish57-bit/aegisgraph-falkordb` (public; README includes setup, safety properties, limitations and the Rule 11 AI disclosure)

**5. Demo video**
`<VIDEO_URL>` (script below)

**6. How did you use FalkorDB?**
FalkorDB is the system of record and the compute layer; without it localization, handoffs and ownership
tracing stop working. (1) *Algorithms as tools:* `algo.pageRank`, `betweenness`, `WCC`, `labelPropagation`
and `algo.SPpaths` (latency-weighted paths) run over live topology via the API and an MCP server;
`HarmonicCentrality` is attempted but is not registered in `falkordb/falkordb:v4.14.9` or FalkorDBLite 0.10.0, so the app reports it unavailable. (2) *Multi-hop
Cypher:* bounded variable-length paths constrained by relationship status link an incident's affected
service to downstream degraded services, their deployments inside the incident window, authors, owning teams
and accepted ADRs; reverse reachability computes blast radius. (3) *Indexes:* a vector index
(`dimension 8, cosine`, queried with `db.idx.vector.queryNodes`) over episodes/playbooks/documents, and a
full-text index (`db.idx.fulltext`) over runbooks. (4) *Persisted coordination:* each stage writes an
`AgentTask` linked by `HANDOFF_TO`, and the next stage reads its predecessor from the graph, so an
interrupted run resumes from stored state; retention scores episodes and archives (not deletes) only
unprotected ones. (5) *Isolation:* one graph per tenant via `select_graph`, with validated, allow-listed
tenant ids. Simulation mutates topology and records its outcome in a single Cypher write.

## Part 2

**7. What did you like about FalkorDB?**
Running graph algorithms, variable-length traversals and vector search in the same Cypher session meant an
agent turn needed no export step or second database. `select_graph` made per-tenant isolation trivial, and the
embedded FalkorDBLite let us run real integration tests (including vector and algorithm calls) in CI-style
tests without Docker.

**8. Improvements you would like**
(1) Pre-filter predicates inside `db.idx.vector.queryNodes` so hybrid property + vector queries don't need
over-fetching and post-filtering. (2) Documented score semantics for vector queries (we found cosine
*distance*, lower is closer) and for `algo.SPpaths` option names in one reference page. (3) Availability of the documented
`algo.HarmonicCentrality` in the released image (it was not registered in v4.14.9), or a
capability-listing procedure so apps can feature-detect instead of catching errors.

## Demo video script (~3 min)

| Time | Show | Say |
| --- | --- | --- |
| 0:00 | Dashboard after **Seed demo**; topology, P0 incident | "One FalkorDB graph: services, commits, owners, ADRs, runbooks, memory. Synthetic data." |
| 0:20 | Algorithm capability cards | "PageRank, betweenness, WCC, communities, weighted shortest paths run in-database. Harmonic centrality isn't registered in this FalkorDB release, so it is reported unavailable — we don't fake it." |
| 0:45 | **Investigate** → stage trace | "Locate, Suggest, Propose are persisted AgentTask nodes. Each reads the previous one from the graph." |
| 1:15 | Proposal panel | "Payment-processor, commit a8f921c, owner team, ADR-042, blast radius and evidence path — a heuristic ranking, not proof." |
| 1:40 | Click Simulate before approval → rejected; then approve → simulate | "No approval, no action. Simulation isolates only edges into the approved service; telemetry and incident status are unchanged." |
| 2:10 | Re-run algorithms / shortest paths | "The path through payment is gone: the topology change is real graph state." |
| 2:25 | Memory: retention preview → archive | "Dry run first; protected evidence is never archived." |
| 2:40 | Company brain search + (optional) `docker exec`/Browser Cypher | "Runbooks via full-text, ownership via graph edges. Tenant graphs are isolated with select_graph." |
| 2:55 | README safety + tests | "12 tests, 6 against real embedded FalkorDB." |

## Blog post draft (Dev.to / Hashnode)

**Title:** Why a gateway timeout needs a graph, not a vector search — building an incident investigator on FalkorDB

**1. The problem.** A checkout 504 is three hops away from the cause: `checkout → payment-processor →
redis`, where a commit removed a pool timeout in violation of an ADR. Embedding search over runbooks finds
text about timeouts; it cannot tell you *which deployed commit, who owns it, and which decision governs it*.

**2. The model.** `Service-[DEPENDS_ON]->Service`, `Commit-[DEPLOYED_TO]->Service`, `Person-[AUTHORED]->Commit`,
`Team-[OWNS]->Service`, `Decision-[GOVERNS]->Service`, `Incident-[AFFECTS]->Service`.

**3. Localization query (simplified).**
```cypher
MATCH (i:Incident {id:$id})-[:AFFECTS]->(entry:Service)
MATCH p=(entry)-[:DEPENDS_ON*0..3]->(s:Service)
WHERE all(r IN relationships(p) WHERE r.status='ACTIVE') AND s.status IN ['CRITICAL','DEGRADED']
MATCH (c:Commit)-[d:DEPLOYED_TO]->(s)
WHERE d.deployed_at<=i.created_at AND d.deployed_at>=i.created_at-86400000
RETURN s.name, c.id, c.risk_score ORDER BY c.risk_score DESC
```
Honest caveat: the score is a seeded heuristic, so the output is a *ranked hypothesis with evidence*, not a proof.

**4. Persisted handoffs.** Each stage `MERGE`s an `AgentTask` and `HANDOFF_TO` edge; the next stage reads
from the graph. We test it by killing the process after `LOCATE` and resuming.

**5. Simulation that changes topology.** Approved mitigation replaces `DEPENDS_ON` edges with
`ISOLATED_DEPENDENCY` and writes the outcome in one query; `algo.SPpaths` then returns fewer paths.

**6. Memory without self-deception.** Unexecuted proposals are never stored as successes; retention
archives only unprotected episodes after a grace period, dry-run first.

**7. Lessons.** Vector index scores are cosine *distance* (lower is closer); feature-detect algorithm procedures because builds differ
(HarmonicCentrality was not registered in v4.14.9); don't swallow connection errors into a fake local fallback.

**8. Limits.** Deterministic stages, synthetic data, keyword vectors, no live executor.
