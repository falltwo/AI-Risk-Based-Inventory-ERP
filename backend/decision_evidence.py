"""可驗證 AI 決策紀錄。

AI 回覆是「建議」而非執行指令。這個模組保存不可變的資料證據快照，
驗證模型輸出結構，並記錄人員最後採納、拒絕與觀察到的結果。
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import uuid
from typing import Any, Mapping

from backend import database
from backend.access_control import (
    DECISION_EVIDENCE_READ,
    DECISION_RECORD_WRITE,
    require_capability,
)


OUTPUT_SCHEMA_VERSION = 1
_ALLOWED_RECOMMENDATIONS = {
    "monitor",
    "request_review",
    "propose_alternative_purchase",
}
_ALLOWED_DECISIONS = {"adopted", "rejected", "needs_more_evidence"}
_ALLOWED_FEEDBACK = {"effective", "ineffective", "inconclusive"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def validate_ai_output(output: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only the small, reviewable AI recommendation contract."""
    if not isinstance(output, Mapping):
        raise ValueError("AI 輸出必須是 JSON 物件。")
    recommendation = str(output.get("recommendation", "")).strip()
    reasoning = str(output.get("reasoning", "")).strip()
    limitations = str(output.get("limitations", "")).strip()
    evidence_ids = output.get("evidence_ids", [])
    risk_level = str(output.get("risk_level", "")).strip().lower()
    if recommendation not in _ALLOWED_RECOMMENDATIONS:
        raise ValueError("AI recommendation 不在允許的決策類型內。")
    if not reasoning or not limitations:
        raise ValueError("AI 輸出必須包含 reasoning 與 limitations。")
    if risk_level not in {"low", "medium", "high"}:
        raise ValueError("AI 輸出 risk_level 必須是 low、medium 或 high。")
    if not isinstance(evidence_ids, list) or not all(
        isinstance(item, str) and item.strip() for item in evidence_ids
    ):
        raise ValueError("AI 輸出 evidence_ids 必須是非空字串陣列。")
    return {
        "recommendation": recommendation,
        "reasoning": reasoning,
        "risk_level": risk_level,
        "evidence_ids": list(evidence_ids),
        "limitations": limitations,
    }


def _validate_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise ValueError("evidence snapshot 必須是 JSON 物件。")
    score = snapshot.get("risk_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
        raise ValueError("risk_score 必須是 0 到 100 的數字。")
    data_as_of = str(snapshot.get("data_as_of", "")).strip()
    sources = snapshot.get("sources")
    if not data_as_of:
        raise ValueError("evidence snapshot 必須包含 data_as_of。")
    if not isinstance(sources, list) or not sources:
        raise ValueError("evidence snapshot 必須至少包含一個資料來源。")
    normalized_sources = []
    for source in sources:
        if not isinstance(source, Mapping):
            raise ValueError("每個資料來源必須是 JSON 物件。")
        name = str(source.get("name", "")).strip()
        as_of = str(source.get("as_of", "")).strip()
        if not name or not as_of:
            raise ValueError("資料來源必須包含 name 與 as_of。")
        normalized_sources.append({"name": name, "as_of": as_of})
    return {
        "risk_score": float(score),
        "data_as_of": data_as_of,
        "sources": normalized_sources,
        "affected_entity": str(snapshot.get("affected_entity", "")).strip(),
    }


def create_decision_record(
    *,
    actor: str,
    decision_type: str,
    model_name: str,
    ai_output: Mapping[str, Any],
    evidence_snapshot: Mapping[str, Any],
    decision_id: str | None = None,
) -> dict[str, Any]:
    """Store validated AI output and immutable evidence from the same moment."""
    principal = require_capability(actor, DECISION_RECORD_WRITE)
    structured_output = validate_ai_output(ai_output)
    snapshot = _validate_snapshot(evidence_snapshot)
    decision_type = str(decision_type or "").strip()
    model_name = str(model_name or "").strip()
    if not decision_type or not model_name:
        raise ValueError("decision_type 與 model_name 不可空白。")
    decision_id = str(decision_id or f"DEC-{uuid.uuid4().hex[:12]}").strip()
    now = _now()
    with database.transaction(immediate=True) as conn:
        conn.execute(
            """INSERT INTO decision_records (
                   decision_id, organization_id, created_by, decision_type, model_name,
                   model_output_json, output_schema_version, status, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?)""",
            (
                decision_id,
                principal.organization_id,
                principal.username,
                decision_type,
                model_name,
                _canonical_json(structured_output),
                OUTPUT_SCHEMA_VERSION,
                now,
            ),
        )
        conn.execute(
            """INSERT INTO decision_evidence_snapshots (
                   decision_id, snapshot_json, snapshot_digest, data_as_of, created_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (decision_id, _canonical_json(snapshot), _digest(snapshot), snapshot["data_as_of"], now),
        )
    return get_decision_record(actor=actor, decision_id=decision_id)


def get_decision_record(*, actor: str, decision_id: str) -> dict[str, Any]:
    principal = require_capability(actor, DECISION_EVIDENCE_READ)
    with sqlite3.connect(database.DB_FILE) as conn:
        row = conn.execute(
            """SELECT r.decision_id, r.organization_id, r.created_by, r.decision_type,
                      r.model_name, r.model_output_json, r.output_schema_version, r.status,
                      r.decision_reason, r.decided_by, r.decided_at, r.created_at,
                      s.snapshot_json, s.snapshot_digest, s.data_as_of
               FROM decision_records r JOIN decision_evidence_snapshots s
                 ON s.decision_id = r.decision_id
               WHERE r.decision_id = ?""",
            (decision_id,),
        ).fetchone()
        if row is None or row[1] != principal.organization_id:
            raise ValueError("找不到決策紀錄。")
        feedback = conn.execute(
            """SELECT outcome, note, recorded_by, recorded_at
               FROM decision_feedback WHERE decision_id = ? ORDER BY feedback_id""",
            (decision_id,),
        ).fetchall()
    return {
        "decision_id": row[0], "organization_id": row[1], "created_by": row[2],
        "decision_type": row[3], "model_name": row[4],
        "ai_output": json.loads(row[5]), "output_schema_version": row[6],
        "status": row[7], "decision_reason": row[8], "decided_by": row[9],
        "decided_at": row[10], "created_at": row[11],
        "evidence_snapshot": json.loads(row[12]), "snapshot_digest": row[13],
        "data_as_of": row[14],
        "feedback": [
            {"outcome": item[0], "note": item[1], "recorded_by": item[2], "recorded_at": item[3]}
            for item in feedback
        ],
    }


def list_decision_records(*, actor: str, limit: int = 100) -> list[dict[str, Any]]:
    principal = require_capability(actor, DECISION_EVIDENCE_READ)
    with sqlite3.connect(database.DB_FILE) as conn:
        rows = conn.execute(
            """SELECT r.decision_id FROM decision_records r
               WHERE r.organization_id = ? ORDER BY r.created_at DESC LIMIT ?""",
            (principal.organization_id, max(1, min(int(limit), 200))),
        ).fetchall()
    return [get_decision_record(actor=actor, decision_id=row[0]) for row in rows]


def decide_decision_record(*, actor: str, decision_id: str, outcome: str, reason: str = "") -> dict[str, Any]:
    principal = require_capability(actor, DECISION_RECORD_WRITE)
    outcome = str(outcome or "").strip()
    if outcome not in _ALLOWED_DECISIONS:
        raise ValueError("決定結果不合法。")
    if outcome in {"rejected", "needs_more_evidence"} and not str(reason).strip():
        raise ValueError("拒絕或要求更多證據時必須填寫原因。")
    with database.transaction(immediate=True) as conn:
        record = conn.execute("SELECT status, organization_id FROM decision_records WHERE decision_id = ?", (decision_id,)).fetchone()
        if record is None or record[1] != principal.organization_id:
            raise ValueError("找不到可決定的決策紀錄。")
        if record[0] != "proposed":
            raise ValueError("此決策已完成處理，不能重複決定。")
        conn.execute(
            """UPDATE decision_records SET status=?, decision_reason=?, decided_by=?, decided_at=?
               WHERE decision_id=?""",
            (outcome, str(reason).strip(), principal.username, _now(), decision_id),
        )
    return get_decision_record(actor=actor, decision_id=decision_id)


def add_outcome_feedback(*, actor: str, decision_id: str, outcome: str, note: str = "") -> dict[str, Any]:
    principal = require_capability(actor, DECISION_RECORD_WRITE)
    outcome = str(outcome or "").strip()
    if outcome not in _ALLOWED_FEEDBACK:
        raise ValueError("回饋結果不合法。")
    with database.transaction(immediate=True) as conn:
        record = conn.execute("SELECT status, organization_id FROM decision_records WHERE decision_id = ?", (decision_id,)).fetchone()
        if record is None or record[1] != principal.organization_id:
            raise ValueError("找不到決策紀錄。")
        if record[0] != "adopted":
            raise ValueError("只有已採納的建議可新增結果回饋。")
        conn.execute(
            """INSERT INTO decision_feedback (decision_id, outcome, note, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?)""",
            (decision_id, outcome, str(note).strip(), principal.username, _now()),
        )
    return get_decision_record(actor=actor, decision_id=decision_id)
