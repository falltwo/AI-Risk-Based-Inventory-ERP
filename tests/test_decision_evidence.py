import pytest

from backend import database
from backend.decision_evidence import (
    add_outcome_feedback,
    build_heatmap_alert_draft,
    build_what_if_decision_draft,
    create_decision_record,
    decide_decision_record,
    get_decision_record,
    validate_ai_output,
)


@pytest.fixture
def decision_db(tmp_path, monkeypatch):
    db_path = tmp_path / "decision-evidence.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    database.init_db()
    return db_path


def _output(**overrides):
    value = {
        "recommendation": "propose_alternative_purchase",
        "reasoning": "供應商延遲事件與風險分數皆上升。",
        "risk_level": "high",
        "evidence_ids": ["risk-map:tw-central", "news:123"],
        "limitations": "新聞資料可能延遲，需人工確認。",
    }
    value.update(overrides)
    return value


def _snapshot():
    return {
        "risk_score": 82,
        "data_as_of": "2026-09-14T12:00:00+00:00",
        "sources": [
            {"name": "供應鏈風險地圖", "as_of": "2026-09-14T12:00:00+00:00"},
            {"name": "新聞事件", "as_of": "2026-09-14T11:30:00+00:00"},
        ],
        "affected_entity": "SUP-021 / P-100",
    }


def test_decision_keeps_verified_snapshot_through_human_decision(decision_db):
    created = create_decision_record(
        actor="planner", decision_type="supply_chain_risk_response",
        model_name="gemini-test", ai_output=_output(), evidence_snapshot=_snapshot(),
        decision_id="DEC-TEST-1",
    )
    assert created["status"] == "proposed"
    assert created["snapshot_digest"]
    assert created["evidence_snapshot"]["risk_score"] == 82

    decided = decide_decision_record(
        actor="planner", decision_id="DEC-TEST-1", outcome="adopted"
    )
    assert decided["status"] == "adopted"
    assert decided["evidence_snapshot"] == created["evidence_snapshot"]
    assert decided["snapshot_digest"] == created["snapshot_digest"]


def test_invalid_structured_output_and_invalid_snapshot_are_rejected(decision_db):
    with pytest.raises(ValueError, match="recommendation"):
        validate_ai_output(_output(recommendation="free-form-action"))
    with pytest.raises(ValueError, match="risk_score"):
        create_decision_record(
            actor="planner", decision_type="supply_chain_risk_response",
            model_name="gemini-test", ai_output=_output(),
            evidence_snapshot={**_snapshot(), "risk_score": 101},
        )


def test_what_if_response_becomes_reviewable_but_unpersisted_draft():
    draft = build_what_if_decision_draft(
        question="紅海航線中斷兩週，哪些採購單會受影響？",
        answer="採購單 PO-101 與 PO-102 可能延遲，建議人工覆核。",
        model_name="gemini/gemini-2.5-flash",
    )

    assert draft["decision_type"] == "supply_chain_what_if_response"
    assert draft["ai_output"]["recommendation"] == "request_review"
    assert draft["ai_output"]["reasoning"].startswith("採購單 PO-101")
    assert draft["evidence_snapshot"]["risk_score"] == 50
    assert "紅海航線" in draft["evidence_snapshot"]["affected_entity"]
    assert draft["ai_output"] == validate_ai_output(draft["ai_output"])

    with pytest.raises(ValueError, match="分析失敗"):
        build_what_if_decision_draft(
            question="測試", answer="模擬分析暫時無法產生：缺少金鑰", model_name="gemini/test"
        )


def test_high_risk_heatmap_node_becomes_reviewable_alert_draft():
    draft = build_heatmap_alert_draft(
        region_name="越南 東南亞",
        risk_score=88,
        ai_summary="港口罷工可能延誤零件交期。",
        data_as_of="2026-09-14T12:00:00+00:00",
    )
    assert draft["decision_type"] == "supply_chain_heatmap_alert"
    assert draft["ai_output"]["recommendation"] == "propose_alternative_purchase"
    assert draft["evidence_snapshot"]["risk_score"] == 88
    assert draft["ai_output"] == validate_ai_output(draft["ai_output"])

    with pytest.raises(ValueError, match="達 70"):
        build_heatmap_alert_draft(region_name="台灣 東亞", risk_score=69)


def test_viewer_cannot_create_or_decide_and_adopted_record_accepts_feedback(decision_db):
    with pytest.raises(PermissionError, match="decision.record.write"):
        create_decision_record(
            actor="viewer", decision_type="supply_chain_risk_response",
            model_name="gemini-test", ai_output=_output(), evidence_snapshot=_snapshot(),
        )
    create_decision_record(
        actor="planner", decision_type="supply_chain_risk_response",
        model_name="gemini-test", ai_output=_output(), evidence_snapshot=_snapshot(),
        decision_id="DEC-TEST-2",
    )
    with pytest.raises(PermissionError, match="decision.record.write"):
        decide_decision_record(actor="viewer", decision_id="DEC-TEST-2", outcome="adopted")
    decide_decision_record(actor="planner", decision_id="DEC-TEST-2", outcome="adopted")
    record = add_outcome_feedback(
        actor="planner", decision_id="DEC-TEST-2", outcome="effective", note="實際延遲已降低。"
    )
    assert record["feedback"][0]["outcome"] == "effective"
    assert get_decision_record(actor="viewer", decision_id="DEC-TEST-2")["status"] == "adopted"


def test_rejection_requires_reason_and_record_cannot_be_decided_twice(decision_db):
    create_decision_record(
        actor="planner", decision_type="supply_chain_risk_response",
        model_name="gemini-test", ai_output=_output(), evidence_snapshot=_snapshot(),
        decision_id="DEC-TEST-3",
    )
    with pytest.raises(ValueError, match="必須填寫原因"):
        decide_decision_record(actor="planner", decision_id="DEC-TEST-3", outcome="rejected")
    decide_decision_record(
        actor="planner", decision_id="DEC-TEST-3", outcome="rejected", reason="成本資料不足。"
    )
    with pytest.raises(ValueError, match="不能重複"):
        decide_decision_record(actor="planner", decision_id="DEC-TEST-3", outcome="adopted")
