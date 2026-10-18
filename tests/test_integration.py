"""Opt-in real database checks. All graphs live in a temporary embedded DB."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.graph_engine import AegisGraphEngine, Conflict, NotFound

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("AEGIS_TEST_GRAPH") != "1", reason="Set AEGIS_TEST_GRAPH=1 to exercise embedded FalkorDB")]


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    from redislite.falkordb_client import FalkorDB
    return FalkorDB(str(tmp_path_factory.mktemp("falkor") / "test.db"))


@pytest.fixture
def graph(database, monkeypatch):
    tenant = "t_" + uuid.uuid4().hex
    monkeypatch.setenv("AEGIS_ALLOWED_TENANTS", tenant)
    e = AegisGraphEngine(db=database)
    e.initialize_tenant_graph(tenant)
    return e, tenant


def test_seed_search_vector_and_algorithms(graph):
    e, t = graph
    assert e.initialize_tenant_graph(t)["status"] == "already_initialized"
    assert len([n for n in e.graph_snapshot(t)["nodes"] if n["label"] == "Service"]) == 8
    assert e.search(t, "payment")["documents"][0]["id"] == "DOC-101"
    results = e.algorithms(t)
    assert results["pagerank"]["available"]
    assert len(results["pagerank"]["rows"]) == 8
    assert results["shortest_paths"]["available"]
    distances = e._q(t, "CALL db.idx.vector.queryNodes('Episode','embedding',2,vecf32($v)) "
                     "YIELD node,score RETURN node.id AS id,score ORDER BY score",
                     {"v": __import__('backend.graph_engine', fromlist=['text_to_vec8']).text_to_vec8(
                         "Historical synthetic payment Redis retry storm: circuit breaker reduced latency. Not current recovery evidence.")})
    assert distances[0]["id"] == "EP-1001"
    assert abs(distances[0]["score"]) < 1e-5


def test_approved_simulation_is_scoped_idempotent_and_honest(graph):
    e, t = graph
    before = e._q(t, "MATCH (s:Service) RETURN s.id AS id,s.status AS status,s.latency_ms AS latency ORDER BY id")
    w = e.investigate(t, "INC-2026-901")
    assert w["status"] == "PROPOSED"
    assert w["proposal"]["service_id"] == "svc_payment"
    assert len(w["proposal"]["blast_radius"]) == 2
    assert [s["stage"] for s in w["stages"]] == ["LOCATE", "SUGGEST", "PROPOSE"]
    with pytest.raises(Conflict):
        e.simulate(t, w["id"])
    assert not any(m["kind"] == "SIMULATED_OUTCOME" for m in e.memories(t))
    e.approve(t, w["id"])
    assert e.simulate(t, w["id"])["status"] == "SIMULATED"
    assert e.simulate(t, w["id"])["status"] == "SIMULATED"
    after = e._q(t, "MATCH (s:Service) RETURN s.id AS id,s.status AS status,s.latency_ms AS latency ORDER BY id")
    assert before == after
    active = e._q(t, "MATCH (a)-[r:DEPENDS_ON]->(s:Service {id:'svc_payment'}) RETURN count(r) AS count")
    assert active[0]["count"] == 0
    assert e._q(t, "MATCH ()-[r:DEPENDS_ON]->() RETURN count(r) AS count")[0]["count"] == 7
    assert len(e.algorithms(t)["shortest_paths"]["rows"]) == 1
    outcomes = [m for m in e.memories(t) if m["kind"] == "SIMULATED_OUTCOME"]
    assert len(outcomes) == 1 and "No production command executed" in outcomes[0]["summary"]
    assert e.incidents(t)[0]["status"] == "OPEN"


def test_missing_evidence_and_future_deploy_never_propose(graph):
    e, t = graph
    with pytest.raises(NotFound):
        e.investigate(t, "missing")
    e._q(t, "MATCH ()-[d:DEPLOYED_TO]->() SET d.deployed_at=9999999999999", write=True)
    w = e.investigate(t, "INC-2026-901")
    assert w["status"] == "INSUFFICIENT_EVIDENCE"
    assert w["proposal"] is None
    with pytest.raises(Conflict):
        e.approve(t, w["id"])


def test_resume_uses_persisted_stage_after_interruption(graph, monkeypatch):
    e, t = graph
    original = e._stage

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        if args[2] == "LOCATE":
            raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(e, "_stage", interrupted)
    with pytest.raises(RuntimeError):
        e.investigate(t, "INC-2026-901")
    trace = e._q(t, "MATCH (w:Investigation) RETURN w.id AS id")[0]["id"]
    restarted = AegisGraphEngine(db=e.db)
    assert restarted.resume(t, trace)["status"] == "PROPOSED"
    assert len(restarted.get_investigation(t, trace)["stages"]) == 3
    assert restarted.resume(t, trace)["status"] == "PROPOSED"


def test_archive_dry_run_and_protected_evidence(graph):
    e, t = graph
    e.investigate(t, "INC-2026-901")
    result = e.retain(t)
    assert [c["id"] for c in result["candidates"]] == ["EP-1002"]
    assert not any(m["archived"] for m in e.memories(t))
    assert e.retain(t, dry_run=False)["archived"] == [{"id": "EP-1002"}]
    assert not e.retain(t, threshold=1, dry_run=False)["archived"]
    assert next(m for m in e.memories(t) if m["id"] == "EP-1001")["protected"]


def test_unique_concurrent_workflows_and_tenant_separation(graph, monkeypatch):
    e, t = graph
    with ThreadPoolExecutor(max_workers=3) as pool:
        workflows = list(pool.map(lambda _: e.investigate(t, "INC-2026-901"), range(3)))
    assert len({w["id"] for w in workflows}) == 3
    other = t + "b"
    monkeypatch.setenv("AEGIS_ALLOWED_TENANTS", t + "," + other)
    e.initialize_tenant_graph(other)
    with pytest.raises(NotFound):
        e.get_investigation(other, workflows[0]["id"])
