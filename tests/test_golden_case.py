"""End-to-end: the demo case must produce exactly the verdict shown in the mockup."""
from conftest import RING


def verdicts(golden):
    return {v.node_id: v for v in golden["state"]["verdict"].node_verdicts}


def test_traces_all_ten_ring_accounts(golden):
    assert sorted(golden["state"]["ring"]) == sorted(RING)


def test_cycle_back_to_A_is_found(golden):
    texts = " ".join(e.text for e in golden["state"]["evidence"] if e.kind == "cycle")
    assert "C6" in texts and "A" in texts


def test_shared_device_D4471(golden):
    ev = [e for e in golden["state"]["evidence"] if "D-4471" in e.text]
    assert ev, "device D-4471 evidence missing"
    assert {"B3", "C5", "C6"} <= set(ev[0].nodes)


def test_merchant_C3_is_defended(golden):
    v = verdicts(golden)
    assert v["C3"].tier == 2, "the 4-year GST kirana store must only get step-up"
    assert "C3" in golden["state"]["verdict"].upheld_objections


def test_ringleaders_hard_frozen(golden):
    v = verdicts(golden)
    for acc in ["A", "B1", "B2", "B3"]:
        assert v[acc].tier == 4, acc


def test_mules_soft_frozen(golden):
    v = verdicts(golden)
    for acc in ["C1", "C2", "C4", "C5", "C6"]:
        assert v[acc].tier >= 3, acc


def test_confident_autonomous_route(golden):
    verdict = golden["state"]["verdict"]
    assert verdict.confidence >= 0.85
    assert verdict.counterfactual_ok
    assert golden["state"]["route"] == "act"


def test_every_citation_points_to_real_evidence(golden):
    known = {e.id for e in golden["state"]["evidence"]}
    s = golden["state"]
    cited = set(s["prosecution"].cited_evidence) | set(s["defense"].cited_evidence) | set(s["verdict"].cited_evidence)
    for v in s["verdict"].node_verdicts:
        cited |= set(v.cited_evidence)
    assert cited, "agents cited nothing"
    assert cited <= known, f"unknown evidence IDs: {cited - known}"


def test_actions_and_sar(golden):
    s = golden["state"]
    touched = {acc for a in s["actions"] for acc in a["accounts"]}
    assert touched == set(RING), "every ring account gets exactly one countermeasure"
    assert all(a["status"] == 200 for a in s["actions"])
    assert s["sar_path"], "SAR was not generated"


def test_audit_chain_intact(golden):
    audit = golden["audit"]
    assert audit.for_case("FR-TEST-GOLD"), "nothing audited"
    assert audit.verify()


def test_audit_detects_tampering(golden, tmp_path):
    import shutil, sqlite3
    from security.audit import AuditLog
    copy = tmp_path / "tampered.db"
    shutil.copy(golden["audit"].path, copy)
    with sqlite3.connect(copy) as db:
        db.execute("UPDATE audit SET payload = '{\"tier\": 0}' WHERE id = (SELECT MIN(id) FROM audit)")
    assert not AuditLog(copy).verify()
