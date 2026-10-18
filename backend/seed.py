"""Synthetic, explicitly attributed demo evidence; no production integrations."""

SERVICES = [
    ("svc_edge", "api-gateway", "DEGRADED", 0.14, 420),
    ("svc_auth", "auth-service", "HEALTHY", 0.002, 18),
    ("svc_checkout", "checkout-service", "DEGRADED", 0.28, 890),
    ("svc_payment", "payment-processor", "CRITICAL", 0.64, 2450),
    ("svc_cache", "redis-session-cluster", "DEGRADED", 0.35, 610),
    ("svc_inventory", "inventory-service", "HEALTHY", 0.005, 25),
    ("svc_ledger", "ledger-db", "HEALTHY", 0.001, 12),
    ("svc_fraud", "fraud-scoring-engine", "HEALTHY", 0.01, 45),
]
DEPENDENCIES = [
    ("svc_edge", "svc_auth", 22), ("svc_edge", "svc_checkout", 180),
    ("svc_checkout", "svc_payment", 490), ("svc_checkout", "svc_inventory", 30),
    ("svc_payment", "svc_cache", 580), ("svc_payment", "svc_ledger", 15),
    ("svc_payment", "svc_fraud", 45), ("svc_auth", "svc_cache", 14),
]
COMPANY = """
MERGE (t:Team {id:'team_payments'}) SET t.name='Checkout & Payments', t.slack_channel='#eng-payments'
MERGE (platform:Team {id:'team_platform'}) SET platform.name='Core Platform & SRE'
MERGE (p:Person {id:'eng_elena'}) SET p.name='Elena Rostova', p.on_call=true
MERGE (a:Person {id:'eng_arjun'}) SET a.name='Arjun Mehta', a.on_call=true
MERGE (p)-[:MEMBER_OF]->(t)
MERGE (a)-[:MEMBER_OF]->(platform)
WITH t, platform, p, a
MATCH (s:Service {id:'svc_payment'}), (cache:Service {id:'svc_cache'})
MERGE (t)-[:OWNS]->(s) MERGE (p)-[:OWNS]->(s)
MERGE (platform)-[:OWNS]->(cache) MERGE (a)-[:OWNS]->(cache)
MERGE (c:Commit {id:'a8f921c'})
SET c.sha='a8f921c', c.message='Remove pool timeout; raise Redis max_retries to 25',
    c.timestamp=$deployed, c.risk_score=0.94
MERGE (p)-[:AUTHORED]->(c)
MERGE (c)-[d:DEPLOYED_TO]->(s) SET d.deployed_at=$deployed, d.environment='demo-prod'
MERGE (adr:Decision {id:'ADR-042'})
SET adr.title='Bounded connection pools and circuit breakers', adr.status='ACCEPTED',
    adr.rationale='Payment max_retries <= 3; circuit breaker <= 250ms', adr.valid_from=$older
MERGE (adr)-[:GOVERNS]->(s) MERGE (a)-[:AUTHORED]->(adr)
MERGE (inc:Incident {id:'INC-2026-901'})
ON CREATE SET inc.symptom='Checkout 504s and Redis connection pool exhaustion',
    inc.severity='P0', inc.status='OPEN', inc.created_at=$now, inc.environment='demo-prod'
MERGE (inc)-[:AFFECTS]->(s)
WITH inc
MATCH (checkout:Service {id:'svc_checkout'}) MERGE (inc)-[:AFFECTS]->(checkout)
"""
