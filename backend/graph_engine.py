"""FalkorDB is the source of truth. Commands are proposals, never executed."""
import hashlib
import json
import math
import os
import re
import time
import uuid
from threading import RLock

from falkordb import FalkorDB
from redis.exceptions import ResponseError

from .seed import COMPANY, DEPENDENCIES, SERVICES


STAGE_ORDER = {"LOCATE": 1, "SUGGEST": 2, "PROPOSE": 3}


class NotFound(ValueError):
    pass


class Conflict(ValueError):
    pass


def tenant_name(tenant):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", tenant):
        raise ValueError("Tenant must be a lowercase identifier, at most 48 characters")
    allowed = os.getenv("AEGIS_ALLOWED_TENANTS", "demo").split(",")
    if tenant not in allowed:
        raise PermissionError("Tenant is not allowed")
    return "aegis_" + tenant


def text_to_vec8(text):
    groups = ("latency timeout slow pool redis", "error crash outage retry", "payment checkout order",
              "auth token session", "commit deploy rollback", "adr decision policy",
              "owner team oncall", "playbook mitigate circuit")
    text = text.lower()
    vector = [sum(word in text for word in group.split()) +
              int(hashlib.sha256(f"{i}:{text}".encode()).hexdigest()[:4], 16) / 655350
              for i, group in enumerate(groups)]
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


def now_ms():
    return int(time.time() * 1000)


class AegisGraphEngine:
    def __init__(self, db=None):
        self._lock = RLock()
        if db is not None:
            self.db = db
        elif os.getenv("GRAPH_MODE", "remote") == "embedded":
            from redislite.falkordb_client import FalkorDB as Embedded
            self.db = Embedded(os.getenv("FALKORDB_PATH", "/tmp/aegisgraph.db"))
        elif os.getenv("GRAPH_MODE", "remote") == "remote":
            self.db = FalkorDB(host=os.getenv("FALKORDB_HOST", "localhost"),
                              port=int(os.getenv("FALKORDB_PORT", "6379")),
                              password=os.getenv("FALKORDB_PASSWORD") or None,
                              socket_connect_timeout=3, socket_timeout=35)
        else:
            raise ValueError("GRAPH_MODE must be remote or embedded")

    def get_graph(self, tenant):
        return self.db.select_graph(tenant_name(tenant))

    def _q(self, tenant, query, params=None, write=False):
        g = self.get_graph(tenant)
        result = (g.query if write else g.ro_query)(query, params or {})
        names = [h[1] for h in result.header]
        return [dict(zip(names, row)) for row in result.result_set]

    def health(self):
        self.db.list_graphs()
        return {"status": "ok", "database": "FalkorDB", "execution_mode": "simulation"}

    def initialize_tenant_graph(self, tenant="demo"):
        # Single-process demo initialization. MERGE makes interrupted seeds recoverable.
        with self._lock:
            graph_name = tenant_name(tenant)
            if graph_name in self.db.list_graphs() and self._q(tenant, "MATCH (m:Schema {id:'v1', ready:true}) RETURN m.id AS id"):
                return {"status": "already_initialized", "tenant": tenant}
            for label in ("Service", "Person", "Team", "Incident", "Commit", "Decision",
                          "Document", "Playbook", "Episode", "AgentTask", "Investigation"):
                self._q(tenant, f"CREATE INDEX FOR (n:{label}) ON (n.id)", write=True)
            for label in ("Document", "Episode", "Playbook"):
                self._q(tenant, f"CREATE VECTOR INDEX FOR (n:{label}) ON (n.embedding) "
                        "OPTIONS {dimension:8, similarityFunction:'cosine'}", write=True)
            self._q(tenant, "CALL db.idx.fulltext.createNodeIndex('Document','title','content')", write=True)
            now = now_ms()
            for sid, name, status, errors, latency in SERVICES:
                self._q(tenant, "MERGE (s:Service {id:$id}) ON CREATE SET s.name=$name, "
                        "s.status=$status, s.error_rate=$errors, s.latency_ms=$latency, s.source='synthetic-demo'",
                        dict(id=sid, name=name, status=status, errors=errors, latency=latency), True)
            for source, target, latency in DEPENDENCIES:
                self._q(tenant, "MATCH (a:Service {id:$a}),(b:Service {id:$b}) "
                        "MERGE (a)-[r:DEPENDS_ON]->(b) ON CREATE SET r.latency_ms=$latency, r.status='ACTIVE'",
                        dict(a=source, b=target, latency=latency), True)
            self._q(tenant, COMPANY, dict(now=now, deployed=now-900000, older=now-86400000), True)
            content = "Inspect payment Redis retry storms. Propose rollback; enforce ADR-042 circuit breaker. Verify telemetry before claiming recovery."
            self._q(tenant, "MATCH (s:Service {id:'svc_payment'}),(p:Person {id:'eng_arjun'}) "
                    "MERGE (d:Document {id:'DOC-101'}) SET d.title='Payment retry storm runbook', "
                    "d.content=$content,d.source='demo://runbooks/payment',d.embedding=vecf32($vec) "
                    "MERGE (d)-[:DOCUMENTS]->(s) MERGE (p)-[:AUTHORED]->(d) "
                    "MERGE (pb:Playbook {id:'PB-01'}) SET pb.name='Isolate payment dependency in simulation', "
                    "pb.action_command='PROPOSED ONLY: rollback a8f921c; enforce MAX_RETRIES=3', "
                    "pb.simulations=0,pb.embedding=vecf32($vec) MERGE (pb)-[:MITIGATES]->(s)",
                    dict(content=content, vec=text_to_vec8(content)), True)
            for eid, summary, age, count in [
                ("EP-1001", "Historical synthetic payment Redis retry storm: circuit breaker reduced latency. Not current recovery evidence.", 86400000, 5),
                ("EP-1002", "Old unrelated staging CSS discussion", 604800000, 0),
            ]:
                self._q(tenant, "MATCH (s:Service {id:'svc_payment'}) MERGE (e:Episode {id:$id}) "
                        "ON CREATE SET e.summary=$summary,e.created_at=$created,e.last_accessed_at=$created, "
                        "e.access_count=$count,e.archived=false,e.protected=false,e.session_id=$id, "
                        "e.embedding=vecf32($vec),e.kind='SYNTHETIC_HISTORY' MERGE (e)-[:RECORDED_IN]->(s)",
                        dict(id=eid, summary=summary, created=now-age, count=count, vec=text_to_vec8(summary)), True)
            self._q(tenant, "MERGE (m:Schema {id:'v1'}) SET m.ready=true", write=True)
            return {"status": "initialized", "tenant": tenant}

    def graph_snapshot(self, tenant):
        nodes = self._q(tenant, "MATCH (n) WHERE NOT n:Schema RETURN n.id AS id,labels(n)[0] AS label, "
                        "coalesce(n.name,n.title,n.id) AS name,n.status AS status LIMIT 500")
        edges = self._q(tenant, "MATCH (a)-[r]->(b) WHERE ID(r)>=0 RETURN a.id AS source,b.id AS target, "
                        "type(r) AS type,r.status AS status LIMIT 1000")
        return {"nodes": nodes, "edges": edges}

    def incidents(self, tenant):
        return self._q(tenant, "MATCH (i:Incident) RETURN i.id AS id,i.symptom AS symptom, "
                       "i.status AS status,i.severity AS severity ORDER BY i.created_at DESC")

    def algorithms(self, tenant):
        queries = {
            "pagerank": "CALL algo.pageRank('Service','DEPENDS_ON') YIELD node,score RETURN node.name AS service,score ORDER BY score DESC",
            "betweenness": "CALL algo.betweenness({nodeLabels:['Service'],relationshipTypes:['DEPENDS_ON'],samplingSeed:42}) YIELD node,score RETURN node.name AS service,score ORDER BY score DESC",
            "harmonic": "CALL algo.HarmonicCentrality({nodeLabels:['Service'],relationshipTypes:['DEPENDS_ON']}) YIELD node,score RETURN node.name AS service,score ORDER BY score DESC",
            "components": "CALL algo.WCC({nodeLabels:['Service'],relationshipTypes:['DEPENDS_ON']}) YIELD node,componentId RETURN componentId,collect(node.name) AS services",
            "communities": "CALL algo.labelPropagation({nodeLabels:['Service','Person','Team'],relationshipTypes:['DEPENDS_ON','OWNS','MEMBER_OF'],maxIterations:10}) YIELD node,communityId RETURN communityId,collect(node.name) AS members",
            "shortest_paths": "MATCH (a:Service {id:'svc_edge'}),(b:Service {id:'svc_cache'}) CALL algo.SPpaths({sourceNode:a,targetNode:b,relTypes:['DEPENDS_ON'],weightProp:'latency_ms',relDirection:'outgoing',pathCount:2}) YIELD path,pathWeight RETURN [n IN nodes(path)|n.name] AS path,pathWeight AS total_latency_ms",
        }
        result = {}
        for name, query in queries.items():
            try:
                result[name] = {"available": True, "rows": self._q(tenant, query)}
            except ResponseError as exc:
                result[name] = {"available": False, "rows": [], "error": str(exc)}
        return result

    def _stage(self, tenant, trace, stage, findings, previous=None):
        self._q(tenant, "MATCH (w:Investigation {id:$trace}) MERGE (t:AgentTask {id:$id}) "
                "SET t.stage=$stage,t.findings=$findings,t.timestamp=$now,t.seq=$seq "
                "MERGE (w)-[:HAS_TASK]->(t)",
                dict(trace=trace, id=f"{trace}_{stage}", stage=stage, findings=json.dumps(findings), now=now_ms(),
                     seq=STAGE_ORDER[stage]), True)
        if previous:
            self._q(tenant, "MATCH (a:AgentTask {id:$a}),(b:AgentTask {id:$b}) MERGE (a)-[:HANDOFF_TO]->(b)",
                    dict(a=f"{trace}_{previous}", b=f"{trace}_{stage}"), True)

    def _read_stage(self, tenant, trace, stage):
        rows = self._q(tenant, "MATCH (t:AgentTask {id:$id}) RETURN t.findings AS findings", dict(id=f"{trace}_{stage}"))
        return json.loads(rows[0]["findings"]) if rows else None

    def investigate(self, tenant, incident_id, user_query=""):
        if not self._q(tenant, "MATCH (i:Incident {id:$id,status:'OPEN'}) RETURN i.id AS id", dict(id=incident_id)):
            raise NotFound("Open incident not found")
        trace = str(uuid.uuid4())
        self._q(tenant, "MATCH (i:Incident {id:$incident}) CREATE (w:Investigation {id:$id, "
                "incident_id:$incident,user_query:$query,status:'OBSERVED',created_at:$now}) CREATE (w)-[:ANALYZED]->(i)",
                dict(id=trace, incident=incident_id, query=user_query, now=now_ms()), True)
        return self.resume(tenant, trace)

    def resume(self, tenant, trace):
        with self._lock:
            workflow = self.get_investigation(tenant, trace)
            if workflow["status"] != "OBSERVED":
                return workflow
            located = self._read_stage(tenant, trace, "LOCATE")
            if located is None:
                candidates = self._q(tenant, """
                    MATCH (i:Incident {id:$id})-[:AFFECTS]->(entry:Service)
                    MATCH p=(entry)-[:DEPENDS_ON*0..3]->(s:Service)
                    WHERE all(r IN relationships(p) WHERE r.status='ACTIVE') AND s.status IN ['CRITICAL','DEGRADED']
                    MATCH (c:Commit)-[d:DEPLOYED_TO]->(s)
                    WHERE d.deployed_at<=i.created_at AND d.deployed_at>=i.created_at-86400000 AND d.environment=i.environment
                    OPTIONAL MATCH (owner:Team)-[:OWNS]->(s)
                    OPTIONAL MATCH (author:Person)-[:AUTHORED]->(c)
                    OPTIONAL MATCH (adr:Decision)-[:GOVERNS]->(s)
                    WHERE adr.status='ACCEPTED' AND adr.valid_from<=i.created_at
                    RETURN DISTINCT s.id AS service_id,s.name AS service,c.id AS commit,c.message AS change,
                        c.risk_score AS heuristic_score,owner.name AS owner,author.name AS author,adr.id AS applicable_adr,
                        [n IN nodes(p)|n.id] AS evidence_path
                    ORDER BY heuristic_score DESC LIMIT 5
                """, dict(id=workflow["incident_id"]))
                located = {"candidates": candidates, "method": "topology + deployment-window heuristic; not causal proof"}
                self._stage(tenant, trace, "LOCATE", located)
            # Every stage consumes its predecessor's persisted output, not transient agent state.
            located = self._read_stage(tenant, trace, "LOCATE")
            if not located["candidates"]:
                self._q(tenant, "MATCH (w:Investigation {id:$id}) SET w.status='INSUFFICIENT_EVIDENCE'", dict(id=trace), True)
                return self.get_investigation(tenant, trace)
            if self._read_stage(tenant, trace, "SUGGEST") is None:
                candidate = located["candidates"][0]
                recalled = self._q(tenant, """
                    CALL db.idx.vector.queryNodes('Episode','embedding',20,vecf32($vec)) YIELD node,score
                    WITH node,score WHERE node.archived=false AND score<=0.5
                    WITH node,score ORDER BY score ASC LIMIT 5
                    RETURN node.id AS id,node.summary AS summary,score AS distance
                """, dict(vec=text_to_vec8(workflow["user_query"] + " " + candidate["change"])))
                context = self._q(tenant, "MATCH (e:Episode)-[*1..2]-(n) WHERE e.id IN $ids "
                                  "RETURN DISTINCT n.id AS id,labels(n)[0] AS label LIMIT 15", dict(ids=[r["id"] for r in recalled]))
                books = self._q(tenant, "MATCH (p:Playbook)-[:MITIGATES]->(s:Service {id:$id}) "
                                "RETURN p.id AS id,p.name AS name,p.action_command AS command ORDER BY p.id LIMIT 3",
                                dict(id=candidate["service_id"]))
                self._stage(tenant, trace, "SUGGEST", {"candidate": candidate, "memories": recalled,
                                                       "context": context, "playbooks": books}, "LOCATE")
            suggestion = self._read_stage(tenant, trace, "SUGGEST")
            self._q(tenant, "MATCH (w:Investigation {id:$id}),(e:Episode) WHERE e.id IN $ids "
                    "MERGE (w)-[r:USES_EVIDENCE]->(e) ON CREATE SET e.access_count=e.access_count+1, "
                    "e.last_accessed_at=$now,e.protected=true",
                    dict(id=trace, ids=[r["id"] for r in suggestion["memories"]], now=now_ms()), True)
            if not suggestion["playbooks"]:
                self._q(tenant, "MATCH (w:Investigation {id:$id}) SET w.status='INSUFFICIENT_EVIDENCE'", dict(id=trace), True)
                return self.get_investigation(tenant, trace)
            candidate, book = suggestion["candidate"], suggestion["playbooks"][0]
            blast = self._q(tenant, "MATCH p=(caller:Service)-[:DEPENDS_ON*1..8]->(s:Service {id:$id}) "
                            "WHERE all(r IN relationships(p) WHERE r.status='ACTIVE') "
                            "RETURN DISTINCT caller.id AS id,caller.name AS service", dict(id=candidate["service_id"]))
            proposal = {**candidate, "playbook_id": book["id"], "command": book["command"],
                        "targets": [candidate["service_id"]], "blast_radius": blast,
                        "evidence": candidate["evidence_path"], "mode": "simulation"}
            self._stage(tenant, trace, "PROPOSE", proposal, "SUGGEST")
            self._q(tenant, "MATCH (w:Investigation {id:$id}) SET w.status='PROPOSED',w.proposal=$proposal",
                    dict(id=trace, proposal=json.dumps(proposal)), True)
            return self.get_investigation(tenant, trace)

    def get_investigation(self, tenant, trace):
        rows = self._q(tenant, "MATCH (w:Investigation {id:$id}) RETURN w.id AS id,w.incident_id AS incident_id, "
                       "w.status AS status,w.user_query AS user_query,w.proposal AS proposal", dict(id=trace))
        if not rows:
            raise NotFound("Investigation not found")
        result = rows[0]
        result["proposal"] = json.loads(result["proposal"]) if result["proposal"] else None
        stages = self._q(tenant, "MATCH (w:Investigation {id:$id})-[:HAS_TASK]->(t:AgentTask) "
                         "RETURN t.stage AS stage,t.findings AS findings,t.timestamp AS timestamp ORDER BY t.seq", dict(id=trace))
        result["stages"] = [{**s, "findings": json.loads(s["findings"])} for s in stages]
        return result

    def approve(self, tenant, trace):
        self.get_investigation(tenant, trace)
        rows = self._q(tenant, "MATCH (w:Investigation {id:$id,status:'PROPOSED'}) "
                       "SET w.status='APPROVED',w.approved_at=$now RETURN w.id AS id", dict(id=trace, now=now_ms()), True)
        if not rows:
            raise Conflict("Only a proposed investigation can be approved")
        return self.get_investigation(tenant, trace)

    def simulate(self, tenant, trace):
        with self._lock:
            w = self.get_investigation(tenant, trace)
            if w["status"] == "SIMULATED":
                return w
            if w["status"] != "APPROVED":
                raise Conflict("Explicit approval is required before simulation")
            p = w["proposal"]
            summary = f"SIMULATION ONLY: isolated incoming dependency edges to {p['service']}. Observed telemetry unchanged. No production command executed."
            # One graph write: scoped active-topology change and outcome are committed together.
            rows = self._q(tenant, """
                MATCH (w:Investigation {id:$id,status:'APPROVED'})-[:ANALYZED]->(inc:Incident {status:'OPEN'})
                MATCH (pb:Playbook {id:$pb}),(s:Service {id:$service})
                MATCH (caller:Service)-[r:DEPENDS_ON]->(s)
                CREATE (caller)-[:ISOLATED_DEPENDENCY {latency_ms:r.latency_ms,status:'ISOLATED',simulation_id:$id}]->(s)
                DELETE r
                WITH DISTINCT w,inc,pb,s
                SET w.status='SIMULATED',w.completed_at=$now,inc.simulation_status='MITIGATED',pb.simulations=pb.simulations+1
                CREATE (ep:Episode {id:$ep,summary:$summary,kind:'SIMULATED_OUTCOME',session_id:$id,
                    created_at:$now,last_accessed_at:$now,access_count:0,protected:true,archived:false,embedding:vecf32($vec)})
                CREATE (ep)-[:RECORDED_IN]->(inc) CREATE (w)-[:PRODUCED]->(ep)
                RETURN w.id AS id
            """, dict(id=trace, pb=p["playbook_id"], service=p["service_id"], now=now_ms(),
                      ep=str(uuid.uuid4()), summary=summary, vec=text_to_vec8(summary)), True)
            if not rows:
                raise Conflict("Incident or active topology changed; investigate again")
            return self.get_investigation(tenant, trace)

    def memories(self, tenant):
        return self._q(tenant, "MATCH (e:Episode) RETURN e.id AS id,e.summary AS summary,e.kind AS kind, "
                       "e.archived AS archived,e.protected AS protected,e.access_count AS access_count, "
                       "e.importance_score AS importance_score ORDER BY e.created_at DESC LIMIT 200")

    def retain(self, tenant, threshold=0.1, dry_run=True):
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("Threshold must be between 0 and 1")
        candidates = self._q(tenant, """
            MATCH (e:Episode) WHERE e.archived=false AND e.protected=false AND e.created_at<$grace
            WITH e, (1.0/(1.0+toFloat($now-e.last_accessed_at)/3600000.0)) *
                (1.0+log(1.0+toFloat(e.access_count))) *
                (1.0/(1.0+toFloat($now-e.created_at)/86400000.0)) AS score
            WHERE score<$threshold RETURN e.id AS id,e.summary AS summary,score ORDER BY score LIMIT 100
        """, dict(now=now_ms(), grace=now_ms()-86400000, threshold=threshold))
        archived = []
        if not dry_run:
            archived = self._q(tenant, "MATCH (e:Episode) WHERE e.id IN $ids AND e.protected=false AND e.archived=false "
                               "SET e.archived=true,e.archived_at=$now RETURN e.id AS id",
                               dict(ids=[c["id"] for c in candidates], now=now_ms()), True)
        return {"dry_run": dry_run, "candidates": candidates, "archived": archived,
                "policy": "recency × frequency × wall-clock age; protected evidence is never archived"}

    def search(self, tenant, query):
        terms = re.findall(r"[a-zA-Z0-9]+", query)[:12]
        if not terms:
            return {"documents": [], "ownership": []}
        docs = self._q(tenant, "CALL db.idx.fulltext.queryNodes('Document',$q) YIELD node,score "
                       "RETURN node.id AS id,node.title AS title,node.content AS content,node.source AS source,score LIMIT 10",
                       dict(q=" | ".join(t + "*" for t in terms)))
        owners = self._q(tenant, "MATCH (o)-[:OWNS]->(s:Service) WHERE any(t IN $terms WHERE toLower(s.name) CONTAINS t) "
                         "RETURN s.id AS service_id,s.name AS service,o.name AS owner,labels(o)[0] AS owner_type LIMIT 20",
                         dict(terms=[t.lower() for t in terms]))
        return {"documents": docs, "ownership": owners}
