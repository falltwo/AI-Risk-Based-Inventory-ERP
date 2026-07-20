"""Streamlit page for governed CSV exchange with an external ERP.

The V1 contract is intentionally narrow: one CSV row is one purchase order
with one item. Uploading only validates and previews data. The operator must
explicitly stage each batch and every live write still goes through the Tool
Gateway approval ledger.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st

from backend import DB_FILE
from backend.access_control import load_principal
from backend.erp_exchange import (
    build_exchange_operation_id,
    build_purchase_order_template_csv,
    build_receipt_template_csv,
    export_approved_actions_csv,
    list_exchange_records,
    list_exchange_receipts,
    parse_purchase_order_csv,
    reconcile_receipt_csv,
    stage_purchase_order_rows,
)
from backend.tool_gateway import gateway
from frontend.access_navigation import exchange_sections


IMPORT_DISPLAY_COLUMNS = [
    "external_id",
    "po_id",
    "supplier_id",
    "product_id",
    "qty",
    "unit_price",
    "order_date",
    "status",
    "note",
    "supplier_country",
    "supplier_region",
    "supplier_risk_level",
]


def describe_sync_state(record: dict) -> str:
    """Describe durable state without overstating external synchronization."""
    state = record.get("sync_state") or "staged"
    if state == "pending":
        return "等待人工核准（尚未同步）"
    if state == "executing":
        return "核准交易執行中（尚未取得 ERP 回執）"
    if state == "approved":
        return "已核准並產生動作檔（待 ERP 回執）"
    if state == "acknowledged":
        receipt_status = record.get("receipt_status") or "unknown"
        return f"已收到 ERP 回執：{receipt_status}"
    if state == "receipt_rejected":
        return "ERP 已驗證回執：rejected（未接受，需修正後重試）"
    if state == "receipt_error":
        return "ERP 已驗證回執：error（可用新 attempt 重試）"
    if state == "rejected":
        return "本版已拒絕（未同步；請修正資料後匯入新版）"
    return "已寫入暫存區（尚未送審、尚未同步）"


def build_preview_rows(rows: list[dict], supplier_risks: dict[str, dict]) -> list[dict]:
    """Return a non-mutating preview enriched with known supplier risk fields."""
    preview = []
    for row in rows:
        enriched = dict(row)
        risk = supplier_risks.get(row.get("supplier_id"), {})
        enriched.update(
            {
                "supplier_country": risk.get("country") or "未設定",
                "supplier_region": risk.get("region") or "未設定",
                "supplier_risk_level": risk.get("risk_level") or "未設定",
            }
        )
        preview.append(enriched)
    return preview


def _render_proposal_section(
    source_system: str, current_actor: str, current_role: str
) -> None:
    st.download_button(
        "下載採購單 CSV 範本",
        data=build_purchase_order_template_csv(),
        file_name="erp_purchase_order_import_template.csv",
        mime="text/csv",
        key="erp_csv_download_template",
    )
    st.markdown("#### 1. 驗證與預覽")
    uploaded = st.file_uploader(
        "上傳外部 ERP 採購單 CSV",
        type=["csv"],
        key="erp_csv_purchase_order_upload",
        help="檔案必須為 UTF-8；上傳本身不會修改 ERP。",
    )

    parsed_rows = None
    if uploaded is not None:
        try:
            parsed_rows = parse_purchase_order_csv(uploaded.getvalue())
            supplier_ids = sorted({row["supplier_id"] for row in parsed_rows})
            supplier_risks: dict[str, dict] = {}
            if supplier_ids:
                placeholders = ",".join("?" for _ in supplier_ids)
                with sqlite3.connect(DB_FILE) as conn:
                    conn.row_factory = sqlite3.Row
                    risk_rows = conn.execute(
                        "SELECT supplier_id, country, region, risk_level "
                        f"FROM suppliers WHERE supplier_id IN ({placeholders})",
                        tuple(supplier_ids),
                    ).fetchall()
                supplier_risks = {
                    row["supplier_id"]: dict(row) for row in risk_rows
                }
            preview = build_preview_rows(parsed_rows, supplier_risks)
            st.success(f"格式驗證通過，共 {len(preview)} 列；目前尚未寫入。")
            st.dataframe(
                pd.DataFrame(preview)[IMPORT_DISPLAY_COLUMNS],
                use_container_width=True,
                hide_index=True,
            )
        except ValueError as exc:
            st.error(f"CSV 驗證失敗：{exc}")
            parsed_rows = None
        except sqlite3.Error:
            st.error("目前無法讀取供應商風險資料，請稍後再試。")
            parsed_rows = None

    if parsed_rows is not None and st.button(
        "寫入暫存區", type="primary", key="erp_csv_stage_batch"
    ):
        try:
            summary = stage_purchase_order_rows(
                source_system, parsed_rows, actor=current_actor
            )
            st.success(
                "暫存完成："
                f"新增 {summary['inserted']}、更新 {summary['updated']}、"
                f"未變更 {summary['unchanged']}。尚未送審或同步。"
            )
        except (ValueError, PermissionError) as exc:
            st.error(f"無法寫入暫存區：{exc}")
        except sqlite3.Error:
            st.error("暫存區寫入失敗，沒有資料被同步至 ERP。")

    st.markdown("#### 2. 暫存資料與送審")
    records: list[dict] = []
    try:
        records = list_exchange_records(source_system, actor=current_actor)
    except (ValueError, PermissionError) as exc:
        st.warning(f"目前無法讀取暫存資料：{exc}")
    except sqlite3.Error:
        st.error("目前無法讀取 ERP 交換暫存區。")

    if not records:
        st.info("此來源尚無暫存資料。")
        return

    record_rows = [
        {
            "外部識別碼": record["external_id"],
            "採購單號": record["po_id"],
            "版本": record["version"],
            "供應商": record["supplier_id"],
            "風險": record.get("supplier_risk_level") or "未設定",
            "狀態": describe_sync_state(record),
        }
        for record in records
    ]
    st.dataframe(pd.DataFrame(record_rows), use_container_width=True, hide_index=True)

    for record in records:
        state = record.get("sync_state") or "staged"
        label = (
            f"{record['external_id']}｜{record['po_id']}｜"
            f"v{record['version']}｜{describe_sync_state(record)}"
        )
        with st.expander(label):
            st.write(
                {
                    "供應商": record["supplier_id"],
                    "品項": record["product_id"],
                    "數量": record["qty"],
                    "單價": record["unit_price"],
                    "國家／地區": " / ".join(
                        filter(
                            None,
                            [
                                record.get("supplier_country"),
                                record.get("supplier_region"),
                            ],
                        )
                    )
                    or "未設定",
                    "供應商風險": record.get("supplier_risk_level") or "未設定",
                }
            )
            if state == "staged":
                operation_id = build_exchange_operation_id(
                    record["source_system"],
                    record["external_id"],
                    record["version"],
                )
                if st.button(
                    "送人工審批",
                    key=(
                        "erp_csv_submit_"
                        f"{record['source_system']}_{record['external_id']}_"
                        f"{record['version']}"
                    ),
                ):
                    result = gateway.call(
                        "sync_external_purchase_order",
                        {
                            "source_system": record["source_system"],
                            "external_id": record["external_id"],
                        },
                        role=current_role,
                        actor=current_actor,
                        agent_name="procurement_agent",
                        operation_id=operation_id,
                    )
                    if result.status == "pending":
                        st.session_state["erp_csv_notice"] = (
                            f"{record['external_id']} 已送審；審批單 "
                            f"{result.approval_id}。尚未同步。"
                        )
                        st.rerun()
                    elif result.status == "ok":
                        st.session_state["erp_csv_notice"] = (
                            f"{record['external_id']} 已完成既有核准操作。"
                        )
                        st.rerun()
                    else:
                        st.error(result.message or "送審失敗，未同步任何資料。")
            elif state == "pending":
                st.info("等待人工核准；此時尚未同步，也不會出現在動作檔。")
            elif state == "approved":
                st.success("已核准，等待 L3 人員匯出動作檔與處理 ERP 回執。")
            elif state == "acknowledged":
                st.success(describe_sync_state(record))
            elif state == "rejected":
                st.warning("此版本已拒絕。請修正 CSV 並以同一 external_id 匯入新版。")


def _render_export_section(source_system: str, current_actor: str) -> None:
    st.markdown("#### 核准後動作檔")
    st.caption(
        "只有已核准且已產生本機執行收據的版本會被匯出；"
        "每次核准都固定為不可變快照。"
        "下載不代表外部 ERP 已接收；收到回執前狀態仍是待確認。"
    )
    try:
        export_records = list_exchange_records(source_system, actor=current_actor)
        export_rows = [
            {
                "外部識別碼": row["external_id"],
                "版本": row["version"],
                "採購單號": row["po_id"],
                "狀態": describe_sync_state(row),
            }
            for row in export_records
        ]
        if export_rows:
            st.dataframe(
                pd.DataFrame(export_rows), use_container_width=True, hide_index=True
            )
        action_csv = export_approved_actions_csv(source_system, actor=current_actor)
        st.download_button(
            "下載已核准 ERP 動作 CSV",
            data=action_csv,
            file_name=f"approved_erp_actions_{source_system or 'source'}.csv",
            mime="text/csv",
            key="erp_csv_download_approved",
        )
    except (ValueError, PermissionError) as exc:
        st.warning(f"目前無法匯出：{exc}")
    except sqlite3.Error:
        st.error("目前無法建立核准後動作檔。")


def _render_reconcile_section(source_system: str, current_actor: str) -> None:
    st.markdown("#### ERP 回執對帳")
    st.caption(
        "回執固定欄位：source_system、external_id、operation_id、approval_id、"
        "payload_digest、receipt_attempt_id、receipt_status、message、key_id、signature。"
        "receipt_status 僅接受 accepted、rejected 或 error；"
        "signature 必須由外部 ERP 連接器使用預先配置的 HMAC 金鑰產生，"
        "未簽章的人工填表不會被接受。"
    )
    try:
        st.download_button(
            "下載待填寫 ERP 回執範本",
            data=build_receipt_template_csv(source_system, actor=current_actor),
            file_name=f"erp_receipt_template_{source_system or 'source'}.csv",
            mime="text/csv",
            key="erp_csv_download_receipt_template",
            help="由目前已核准動作產生；請填 receipt_status 與 message 後回傳。",
        )
    except (ValueError, PermissionError, RuntimeError, sqlite3.Error) as exc:
        st.info(f"目前無法建立回執範本：{exc}")
    receipt_upload = st.file_uploader(
        "上傳 ERP 回執 CSV",
        type=["csv"],
        key="erp_csv_receipt_upload",
        help="選取檔案不會自動對帳，仍需按下確認按鈕。",
    )
    if st.button(
        "確認並寫入回執對帳",
        type="primary",
        key="erp_csv_reconcile_receipt",
    ):
        if receipt_upload is None:
            st.warning("請先選取 ERP 回執 CSV。")
        else:
            try:
                summary = reconcile_receipt_csv(
                    receipt_upload.getvalue(), actor=current_actor
                )
                st.success(
                    "回執對帳完成："
                    f"新增 {summary['inserted']}、未變更 {summary['unchanged']}。"
                )
            except (ValueError, PermissionError, RuntimeError) as exc:
                st.error(f"回執對帳失敗：{exc}")
            except sqlite3.Error:
                st.error("回執對帳失敗，沒有寫入不完整資料。")

    try:
        receipt_records = list_exchange_receipts(source_system, actor=current_actor)
        acknowledged = [
            {
                "外部識別碼": row["external_id"],
                "操作識別碼": row["operation_id"],
                "回執嘗試": row.get("attempt_count"),
                "回執狀態": row.get("receipt_status"),
                "驗證狀態": row.get("trust_state"),
                "最後提交者": row.get("received_by"),
            }
            for row in receipt_records
        ]
        if acknowledged:
            st.dataframe(
                pd.DataFrame(acknowledged), use_container_width=True, hide_index=True
            )
        else:
            st.info("目前尚未收到此來源的 ERP 回執。")
    except (ValueError, PermissionError, sqlite3.Error):
        st.info("目前無法讀取回執狀態。")


def render(username: str = "") -> None:
    principal = load_principal(username)
    if principal is None:
        st.error("登入身分已失效，無法開啟 ERP CSV 交換。")
        return
    sections = exchange_sections(principal)
    if not sections:
        st.error("此帳號沒有 ERP CSV 交換權限。")
        return

    st.subheader("ERP CSV 交換", divider="blue")
    st.caption(
        "固定欄位交換原型：一列代表一張採購單，且每張單只含一個品項。"
        "上傳只做驗證與風險預覽；按下「寫入暫存區」後才會留下資料。"
    )

    notice = st.session_state.pop("erp_csv_notice", None)
    if notice:
        st.success(notice)
    current_actor = principal.username

    source_system = st.text_input(
        "外部 ERP 來源識別碼",
        value="demo-erp",
        key="erp_csv_source_system",
        help="例如 odoo-prod；僅允許英數字、點、底線與連字號。",
    ).strip()

    labels = {
        "proposal": "L2 匯入、風險預覽與提案",
        "export": "L3 核准後匯出",
        "reconcile": "L3 ERP 回執對帳",
    }
    tabs = st.tabs([labels[section] for section in sections])
    for section, tab in zip(sections, tabs):
        with tab:
            if section == "proposal":
                _render_proposal_section(
                    source_system, current_actor, principal.role
                )
            elif section == "export":
                _render_export_section(source_system, current_actor)
            elif section == "reconcile":
                _render_reconcile_section(source_system, current_actor)
