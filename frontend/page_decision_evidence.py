"""Streamlit surface for reviewable AI decision records."""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from backend.access_control import (
    DECISION_EVIDENCE_READ,
    DECISION_RECORD_WRITE,
    load_principal,
)
from backend.decision_evidence import (
    add_outcome_feedback,
    create_decision_record,
    decide_decision_record,
    list_decision_records,
)


_RECOMMENDATION_LABELS = {
    "monitor": "持續監控",
    "request_review": "請求人工覆核",
    "propose_alternative_purchase": "提出替代採購建議",
}
_STATUS_LABELS = {
    "proposed": "待人員決定",
    "adopted": "已採納",
    "rejected": "已拒絕",
    "needs_more_evidence": "需要更多證據",
}


def _render_record(record: dict, actor: str, can_write: bool) -> None:
    output = record["ai_output"]
    snapshot = record["evidence_snapshot"]
    with st.container(border=True):
        st.markdown(f"#### 🧾 決策 `{record['decision_id']}`｜{_STATUS_LABELS.get(record['status'], record['status'])}")
        left, right = st.columns(2)
        left.metric("風險分數", f"{snapshot['risk_score']:.0f} / 100")
        right.metric("資料截至", snapshot["data_as_of"])
        st.markdown(f"**AI 建議**：{_RECOMMENDATION_LABELS.get(output['recommendation'], output['recommendation'])}")
        st.markdown(f"**依據說明**：{output['reasoning']}")
        st.caption(f"模型：{record['model_name']}｜產生時間：{record['created_at']}")
        st.caption("資料來源：" + "、".join(f"{item['name']}（{item['as_of']}）" for item in snapshot["sources"]))
        st.caption(f"證據快照 digest：`{record['snapshot_digest'][:16]}…`｜限制：{output['limitations']}")

        if record["status"] == "proposed" and can_write:
            reason = st.text_input("決定原因", key=f"decision_reason_{record['decision_id']}")
            adopt, reject, more = st.columns(3)
            if adopt.button("採納", key=f"adopt_{record['decision_id']}", use_container_width=True):
                decide_decision_record(actor=actor, decision_id=record["decision_id"], outcome="adopted")
                st.rerun()
            if reject.button("拒絕", key=f"reject_{record['decision_id']}", use_container_width=True):
                try:
                    decide_decision_record(actor=actor, decision_id=record["decision_id"], outcome="rejected", reason=reason)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            if more.button("要求補證", key=f"more_{record['decision_id']}", use_container_width=True):
                try:
                    decide_decision_record(actor=actor, decision_id=record["decision_id"], outcome="needs_more_evidence", reason=reason)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

        if record["status"] == "adopted" and can_write:
            with st.expander("新增實際結果回饋"):
                result = st.selectbox("觀察結果", ["effective", "ineffective", "inconclusive"], format_func=lambda value: {"effective": "有效", "ineffective": "無效", "inconclusive": "尚無結論"}[value], key=f"feedback_{record['decision_id']}")
                note = st.text_area("回饋說明", key=f"feedback_note_{record['decision_id']}")
                if st.button("儲存回饋", key=f"save_feedback_{record['decision_id']}"):
                    add_outcome_feedback(actor=actor, decision_id=record["decision_id"], outcome=result, note=note)
                    st.rerun()

        if record["decided_by"]:
            st.caption(f"人員決定：{record['decided_by']}｜{record['decided_at']}｜{record['decision_reason'] or '採納未填原因'}")
        for feedback in record["feedback"]:
            st.info(f"結果回饋：{feedback['outcome']}｜{feedback['recorded_by']}｜{feedback['recorded_at']}\n\n{feedback['note']}")


def render(*, username: str) -> None:
    principal = load_principal(username)
    if principal is None or not principal.can(DECISION_EVIDENCE_READ):
        st.error("你沒有查看 AI 決策證據的權限。")
        return
    can_write = principal.can(DECISION_RECORD_WRITE)
    st.markdown("<div class='premium-title'>🧠 AI 決策證據與回饋</div>", unsafe_allow_html=True)
    st.caption("AI 只提出結構化建議；資料快照、人員決定與結果回饋均可追溯。")

    records_tab, create_tab = st.tabs(["決策紀錄", "建立示範建議"])
    with records_tab:
        records = list_decision_records(actor=principal.username)
        if not records:
            st.info("尚無決策紀錄。請由具有 L2 決策權限的人建立第一筆示範建議。")
        for record in records:
            _render_record(record, principal.username, can_write)

    with create_tab:
        if not can_write:
            st.info("你可查看證據與決策，但建立、採納與回饋需要 L2 決策權限。")
            return
        st.caption("這個表單建立一筆可驗證的示範建議；之後會由實際 AI 工作流自動填入相同結構。")
        with st.form("create_decision_record"):
            entity = st.text_input("受影響項目／供應商", placeholder="例如：SUP-021 / 零件 P-100")
            score = st.slider("風險分數", 0, 100, 70)
            source_name = st.text_input("資料來源", value="供應鏈風險地圖")
            as_of = st.text_input("資料截至時間（UTC）", value=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
            recommendation = st.selectbox("AI 建議", list(_RECOMMENDATION_LABELS), format_func=_RECOMMENDATION_LABELS.get)
            risk_level = st.selectbox("AI 判定風險等級", ["low", "medium", "high"], index=2)
            reasoning = st.text_area("AI 依據說明", placeholder="請說明為何提出此建議。")
            limitations = st.text_area("資料限制", value="此建議僅供人工覆核，未直接執行任何 ERP 異動。")
            submit = st.form_submit_button("建立可驗證建議")
        if submit:
            try:
                create_decision_record(
                    actor=principal.username,
                    decision_type="supply_chain_risk_response",
                    model_name="manual-structured-demo",
                    ai_output={
                        "recommendation": recommendation,
                        "reasoning": reasoning,
                        "risk_level": risk_level,
                        "evidence_ids": [f"source:{source_name}"],
                        "limitations": limitations,
                    },
                    evidence_snapshot={
                        "risk_score": score,
                        "data_as_of": as_of,
                        "sources": [{"name": source_name, "as_of": as_of}],
                        "affected_entity": entity,
                    },
                )
                st.success("已建立決策紀錄；可回到「決策紀錄」採納、拒絕或補上結果回饋。")
            except (ValueError, PermissionError) as exc:
                st.error(str(exc))
