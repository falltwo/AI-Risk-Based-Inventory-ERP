"""L2 workbench for turning affected PO lines into governed proposals."""

from __future__ import annotations

import uuid

import pandas as pd
import streamlit as st

from backend.agent_logger import get_pending_approval_by_id
from backend.purchase_proposals import (
    list_alternative_suppliers,
    list_impacted_purchase_options,
    prepare_alternative_purchase_proposal,
    submit_purchase_proposal,
)


def _new_proposal_id() -> str:
    return f"PROP-{uuid.uuid4().hex[:20].upper()}"


def ensure_purchase_proposal_id(state) -> str:
    """Keep one proposal ID stable across Streamlit reruns."""
    proposal_id = state.get("purchase_proposal_id")
    if not proposal_id:
        proposal_id = _new_proposal_id()
        state["purchase_proposal_id"] = proposal_id
    return proposal_id


def start_new_purchase_proposal(state) -> str:
    """Rotate identity only after an explicit new-proposal action."""
    proposal_id = _new_proposal_id()
    state["purchase_proposal_id"] = proposal_id
    state.pop("purchase_proposal_last_id", None)
    state.pop("purchase_proposal_last_approval_id", None)
    return proposal_id


def remember_purchase_proposal_submission(state, proposal_id: str, result) -> bool:
    """Persist durable Gateway identity after submit or an idempotent replay."""
    approval_id = str(getattr(result, "approval_id", "") or "").strip()
    if getattr(result, "status", None) not in {"pending", "ok", "denied"}:
        return False
    if not approval_id:
        return False
    state["purchase_proposal_last_id"] = proposal_id
    state["purchase_proposal_last_approval_id"] = approval_id
    return True


def _render_submission_state() -> bool:
    """Render the durable approval state; return whether a prior submission exists."""
    proposal_id = st.session_state.get("purchase_proposal_last_id")
    approval_id = st.session_state.get("purchase_proposal_last_approval_id")
    if not proposal_id or not approval_id:
        return False

    approval = get_pending_approval_by_id(approval_id)
    if approval is None:
        st.error(f"找不到審批單 `{approval_id}`，請交由管理者確認。")
    elif approval["status"] == "pending":
        st.info(
            f"提案 `{proposal_id}` 已送審（`{approval_id}`）；尚未寫入 ERP。"
        )
    elif approval["status"] == "approved":
        st.success(
            f"提案 `{proposal_id}` 已由 L3 核准，Gateway 已完成受控執行。"
        )
    elif approval["status"] == "rejected":
        st.warning(
            f"提案 `{proposal_id}` 已被拒絕，ERP 未因本提案產生寫入。"
        )
    else:
        st.error(
            f"提案 `{proposal_id}` 的審批狀態為 `{approval['status']}`，請確認稽核紀錄。"
        )

    if st.button("建立下一份替代採購提案", key="purchase_proposal_next"):
        start_new_purchase_proposal(st.session_state)
        st.rerun()
    return True


def render_purchase_proposal_workbench(*, actor: str) -> None:
    """Render affected PO evidence, candidate suppliers, and proposal submit."""
    st.subheader("🧾 替代採購決策提案")
    st.caption(
        "L2 只建立可稽核提案，沒有核准或執行權；L3 核准前不會寫入 ERP。"
    )
    if _render_submission_state():
        return

    try:
        impacted = list_impacted_purchase_options(actor=actor)
    except (PermissionError, ValueError) as exc:
        st.error(str(exc))
        return
    if not impacted:
        st.info("目前沒有含延遲或替代建議的受影響採購單。")
        return

    option_keys = list(range(len(impacted)))
    selected_index = st.selectbox(
        "選擇受影響採購品項",
        option_keys,
        format_func=lambda index: (
            f"{impacted[index]['po_id']}｜"
            f"{impacted[index]['product_id']} {impacted[index].get('product_name') or ''}｜"
            f"明細 #{impacted[index]['source_po_item_id']} × {impacted[index]['qty']}｜"
            f"原供應商 {impacted[index]['original_supplier_id']}｜"
            f"延遲 {impacted[index].get('estimated_delay_days') or 0} 天"
        ),
        key="purchase_proposal_affected_line",
    )
    selected = impacted[selected_index]
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "受影響採購單": selected["po_id"],
                    "原供應商": selected["supplier_name"],
                    "品項": selected.get("product_name") or selected["product_id"],
                    "數量": selected["qty"],
                    "預估延遲天數": selected.get("estimated_delay_days") or 0,
                    "既有應變建議": selected.get("alternative_suggestion") or "—",
                }
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

    try:
        alternatives = list_alternative_suppliers(
            affected_po_id=selected["po_id"],
            product_id=selected["product_id"],
            source_po_item_id=selected["source_po_item_id"],
            actor=actor,
        )
    except (PermissionError, ValueError) as exc:
        st.error(str(exc))
        return
    if not alternatives:
        st.warning("正式供應商主檔中沒有可供應此品項的替代來源。")
        return

    proposal_id = ensure_purchase_proposal_id(st.session_state)
    with st.form("purchase_proposal_form"):
        alternative_index = st.selectbox(
            "替代供應商",
            list(range(len(alternatives))),
            format_func=lambda index: (
                f"{alternatives[index]['supplier_id']} - {alternatives[index]['name']}｜"
                f"{alternatives[index].get('country') or '地區未填'}｜"
                f"風險 {alternatives[index].get('risk_level') or '未評'}｜"
                f"單價 NT${float(alternatives[index]['price']):,.2f}"
            ),
            key="purchase_proposal_alternative_supplier",
        )
        default_reason = str(selected.get("alternative_suggestion") or "").strip()
        reason = st.text_area(
            "提案理由",
            value=default_reason
            or "因供應鏈事件造成延遲，建議改由正式備援供應商供貨。",
            max_chars=1000,
        )
        delay_days = st.number_input(
            "預估延遲天數",
            min_value=0,
            max_value=3650,
            value=int(selected.get("estimated_delay_days") or 0),
            step=1,
        )
        st.caption(f"提案識別碼：`{proposal_id}`")
        if st.form_submit_button(
            "送交 L3 人工核准", type="primary", use_container_width=True
        ):
            alternative = alternatives[alternative_index]
            try:
                proposal = prepare_alternative_purchase_proposal(
                    proposal_id=proposal_id,
                    affected_po_id=selected["po_id"],
                    product_id=selected["product_id"],
                    source_po_item_id=selected["source_po_item_id"],
                    alternative_supplier_id=alternative["supplier_id"],
                    alternative_supplier_product_id=alternative[
                        "supplier_product_id"
                    ],
                    reason=reason,
                    estimated_delay_days=int(delay_days),
                    actor=actor,
                )
                result = submit_purchase_proposal(proposal, actor=actor)
            except (PermissionError, ValueError) as exc:
                st.error(str(exc))
            else:
                if remember_purchase_proposal_submission(
                    st.session_state, proposal.proposal_id, result
                ):
                    if result.status == "pending":
                        st.success(
                            f"提案已送審（`{result.approval_id}`）；尚未寫入 ERP。"
                        )
                    elif result.status == "ok":
                        st.success(
                            f"已恢復提案（`{result.approval_id}`）的完成狀態。"
                        )
                    else:
                        st.warning(
                            f"已恢復提案（`{result.approval_id}`）的拒絕狀態。"
                        )
                    st.rerun()
                else:
                    st.error(result.message or "提案送審失敗。")
