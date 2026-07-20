"""Independent L2 proposal domain plus adapters to the existing Gateway.

The proposal, approval decision, and execution request are different immutable
objects.  The proposal never stores Gateway operation IDs or approval state;
the adapter derives a stable operation ID from the proposal ID when submitting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import hmac
import json
import math
import re
import sqlite3
from types import MappingProxyType
from typing import Mapping

from backend import database
from backend.access_control import (
    APPROVAL_DECIDE,
    ERP_EXCHANGE_PROPOSE,
    PROPOSAL_EVIDENCE_READ,
    load_principal,
    require_capability,
)


PROPOSAL_TYPE = "alternative_purchase_order"
PROPOSAL_SCHEMA_VERSION = 1
EXECUTION_CONTRACT_VERSION = "v1"
_OPERATION_PREFIX = "proposal:create-po:"
_PROPOSAL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


@dataclass(frozen=True)
class PurchaseProposal:
    proposal_id: str
    proposal_type: str
    schema_version: int
    organization_id: str
    proposer_username: str
    proposer_role: str
    affected_po_id: str
    source_po_item_id: int
    proposed_po_id: str
    original_supplier_id: str
    alternative_supplier_id: str
    alternative_supplier_product_id: int
    product_id: str
    qty: int
    unit_price: float
    currency: str
    order_date: str
    proposed_status: str
    reason: str
    estimated_delay_days: int | None
    source_event_id: int | None
    source_po_version: str
    proposal_digest: str
    created_at: str


@dataclass(frozen=True)
class ApprovalDecision:
    proposal_id: str
    outcome: str
    reason: str = ""


@dataclass(frozen=True)
class PurchaseOrderExecutionRequest:
    tool_name: str
    operation_id: str
    contract_version: str
    args: Mapping[str, object]


_PROPOSAL_COLUMNS = (
    "proposal_id",
    "proposal_type",
    "schema_version",
    "organization_id",
    "proposer_username",
    "proposer_role",
    "affected_po_id",
    "source_po_item_id",
    "proposed_po_id",
    "original_supplier_id",
    "alternative_supplier_id",
    "alternative_supplier_product_id",
    "product_id",
    "qty",
    "unit_price",
    "currency",
    "order_date",
    "proposed_status",
    "reason",
    "estimated_delay_days",
    "source_event_id",
    "source_po_version",
    "proposal_digest",
    "created_at",
)


def _required_text(value, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} 不可為空白。")
    return text


def _validate_proposal_id(proposal_id: str) -> str:
    proposal_id = _required_text(proposal_id, "proposal_id")
    if not _PROPOSAL_ID_RE.fullmatch(proposal_id):
        raise ValueError("proposal_id 格式不合法。")
    return proposal_id


def _canonical_digest(payload: dict) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _proposal_payload(proposal: PurchaseProposal | dict) -> dict:
    payload = asdict(proposal) if isinstance(proposal, PurchaseProposal) else dict(proposal)
    payload.pop("proposal_digest", None)
    return payload


def _proposal_digest(proposal: PurchaseProposal | dict) -> str:
    return _canonical_digest(_proposal_payload(proposal))


def proposal_operation_id(proposal_id: str) -> str:
    proposal_id = _validate_proposal_id(proposal_id)
    return f"{_OPERATION_PREFIX}{proposal_id}:{EXECUTION_CONTRACT_VERSION}"


def _proposal_id_from_operation_id(operation_id: str) -> str:
    operation_id = _required_text(operation_id, "operation_id")
    suffix = f":{EXECUTION_CONTRACT_VERSION}"
    if not operation_id.startswith(_OPERATION_PREFIX) or not operation_id.endswith(suffix):
        raise ValueError("operation_id 不是受支援的採購提案操作。")
    proposal_id = operation_id[len(_OPERATION_PREFIX) : -len(suffix)]
    return _validate_proposal_id(proposal_id)


def _source_po_snapshot(
    conn: sqlite3.Connection,
    affected_po_id: str,
    product_id: str,
    source_po_item_id: int | None = None,
) -> dict:
    conn.row_factory = sqlite3.Row
    where = "p.po_id = ? AND i.product_id = ?"
    params: list[object] = [affected_po_id, product_id]
    if source_po_item_id is not None:
        if isinstance(source_po_item_id, bool) or int(source_po_item_id) <= 0:
            raise ValueError("source_po_item_id 格式錯誤。")
        where += " AND i.id = ?"
        params.append(int(source_po_item_id))
    rows = conn.execute(
        """
        SELECT i.id AS source_po_item_id,
               p.po_id, p.supplier_id, p.order_date, p.status,
               p.estimated_delay_days, i.product_id, i.qty, i.unit_price
        FROM purchase_orders p
        JOIN purchase_order_items i ON i.po_id = p.po_id
        WHERE """
        + where
        + """
        ORDER BY i.id
        """,
        tuple(params),
    ).fetchall()
    if not rows:
        raise ValueError("找不到受影響採購單或指定品項。")
    if source_po_item_id is None and len(rows) != 1:
        raise ValueError("採購單含多筆同品項明細，必須指定 source_po_item_id。")
    row = rows[0]
    if str(row["status"] or "").strip() in {"已完成", "已取消"}:
        raise ValueError("受影響採購單已結案，不能建立替代提案。")
    return dict(row)


def source_po_version(
    conn: sqlite3.Connection,
    affected_po_id: str,
    product_id: str,
    source_po_item_id: int | None = None,
) -> str:
    return "sha256:" + _canonical_digest(
        _source_po_snapshot(
            conn, affected_po_id, product_id, source_po_item_id
        )
    )


def _row_to_proposal(row: sqlite3.Row | tuple) -> PurchaseProposal:
    values = dict(row) if isinstance(row, sqlite3.Row) else dict(zip(_PROPOSAL_COLUMNS, row))
    values["schema_version"] = int(values["schema_version"])
    if values["source_po_item_id"] is not None:
        values["source_po_item_id"] = int(values["source_po_item_id"])
    if values["alternative_supplier_product_id"] is not None:
        values["alternative_supplier_product_id"] = int(
            values["alternative_supplier_product_id"]
        )
    values["qty"] = int(values["qty"])
    values["unit_price"] = float(values["unit_price"])
    if values["estimated_delay_days"] is not None:
        values["estimated_delay_days"] = int(values["estimated_delay_days"])
    if values["source_event_id"] is not None:
        values["source_event_id"] = int(values["source_event_id"])
    return PurchaseProposal(**values)


def _load_proposal_with_conn(
    conn: sqlite3.Connection, proposal_id: str
) -> PurchaseProposal | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        f"SELECT {', '.join(_PROPOSAL_COLUMNS)} FROM purchase_proposals WHERE proposal_id = ?",
        (_validate_proposal_id(proposal_id),),
    ).fetchone()
    return _row_to_proposal(row) if row is not None else None


def _assert_proposal_integrity(proposal: PurchaseProposal) -> None:
    if proposal.proposal_type != PROPOSAL_TYPE:
        raise ValueError("不支援的提案類型。")
    if proposal.schema_version != PROPOSAL_SCHEMA_VERSION:
        raise ValueError("不支援的提案 schema 版本。")
    if not hmac.compare_digest(proposal.proposal_digest, _proposal_digest(proposal)):
        raise PermissionError("採購提案內容完整性驗證失敗。")


def prepare_alternative_purchase_proposal(
    *,
    affected_po_id: str,
    product_id: str,
    source_po_item_id: int | None = None,
    alternative_supplier_id: str,
    alternative_supplier_product_id: int | None = None,
    reason: str,
    actor: str,
    proposal_id: str,
    estimated_delay_days: int | None = None,
    source_event_id: int | None = None,
) -> PurchaseProposal:
    """Build one immutable proposal from server-side ERP master data."""
    require_capability(actor, ERP_EXCHANGE_PROPOSE)
    principal = load_principal(actor)
    if principal is None:
        raise PermissionError("提案人身分無效。")

    proposal_id = _validate_proposal_id(proposal_id)
    affected_po_id = _required_text(affected_po_id, "affected_po_id")
    product_id = _required_text(product_id, "product_id")
    alternative_supplier_id = _required_text(
        alternative_supplier_id, "alternative_supplier_id"
    )
    if source_po_item_id is not None:
        if isinstance(source_po_item_id, bool) or int(source_po_item_id) <= 0:
            raise ValueError("source_po_item_id 格式錯誤。")
        source_po_item_id = int(source_po_item_id)
    if alternative_supplier_product_id is not None:
        if (
            isinstance(alternative_supplier_product_id, bool)
            or int(alternative_supplier_product_id) <= 0
        ):
            raise ValueError("alternative_supplier_product_id 格式錯誤。")
        alternative_supplier_product_id = int(alternative_supplier_product_id)
    reason = _required_text(reason, "reason")
    if len(reason) > 1000:
        raise ValueError("reason 不可超過 1000 字。")
    if estimated_delay_days is not None:
        if isinstance(estimated_delay_days, bool):
            raise ValueError("estimated_delay_days 格式錯誤。")
        estimated_delay_days = int(estimated_delay_days)
        if not 0 <= estimated_delay_days <= 3650:
            raise ValueError("estimated_delay_days 超出允許範圍。")
    if source_event_id is not None:
        source_event_id = int(source_event_id)

    with database.transaction() as conn:
        existing = _load_proposal_with_conn(conn, proposal_id)
        if existing is not None:
            _assert_proposal_integrity(existing)
            same_request = (
                hmac.compare_digest(existing.proposer_username, principal.username)
                and hmac.compare_digest(
                    existing.organization_id, principal.organization_id
                )
                and existing.affected_po_id == affected_po_id
                and existing.product_id == product_id
                and existing.alternative_supplier_id == alternative_supplier_id
                and existing.reason == reason
                and existing.estimated_delay_days == estimated_delay_days
                and existing.source_event_id == source_event_id
            )
            if source_po_item_id is not None:
                same_request = same_request and (
                    existing.source_po_item_id == source_po_item_id
                )
            if alternative_supplier_product_id is not None:
                same_request = same_request and (
                    existing.alternative_supplier_product_id
                    == alternative_supplier_product_id
                )
            if not same_request:
                raise ValueError("proposal_id 已綁定另一份採購提案。")
            return existing

        source = _source_po_snapshot(
            conn, affected_po_id, product_id, source_po_item_id
        )
        source_po_item_id = int(source["source_po_item_id"])
        original_supplier_id = str(source["supplier_id"])
        if hmac.compare_digest(original_supplier_id, alternative_supplier_id):
            raise ValueError("替代供應商必須與原供應商不同。")
        alternative_where = "s.supplier_id = ? AND sp.product_id = ?"
        alternative_params: list[object] = [alternative_supplier_id, product_id]
        if alternative_supplier_product_id is not None:
            alternative_where += " AND sp.id = ?"
            alternative_params.append(alternative_supplier_product_id)
        alternative_rows = conn.execute(
            """
            SELECT sp.id AS supplier_product_id, s.is_official, sp.price
            FROM suppliers s
            JOIN supplier_products sp ON sp.supplier_id = s.supplier_id
            WHERE """
            + alternative_where
            + " ORDER BY sp.id",
            tuple(alternative_params),
        ).fetchall()
        if not alternative_rows:
            raise ValueError("替代供應商未提供指定品項。")
        if alternative_supplier_product_id is None and len(alternative_rows) != 1:
            raise ValueError(
                "替代供應商有多筆同品項報價，必須指定 supplier_product_id。"
            )
        alternative = alternative_rows[0]
        alternative_supplier_product_id = int(alternative["supplier_product_id"])
        if int(alternative["is_official"] or 0) != 1:
            raise PermissionError("替代供應商不是有效的正式供應商。")
        unit_price = float(alternative["price"])
        if not math.isfinite(unit_price) or unit_price < 0:
            raise ValueError("供應商主檔單價格式錯誤。")
        if source_event_id is not None:
            if conn.execute(
                "SELECT 1 FROM supply_chain_events WHERE id = ?", (source_event_id,)
            ).fetchone() is None:
                raise ValueError("找不到來源風險事件。")
        source_version = f"sha256:{_canonical_digest(source)}"

    stable_suffix = hashlib.sha256(proposal_id.encode("utf-8")).hexdigest()[:16]
    values = {
        "proposal_id": proposal_id,
        "proposal_type": PROPOSAL_TYPE,
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "organization_id": principal.organization_id,
        "proposer_username": principal.username,
        "proposer_role": principal.role,
        "affected_po_id": affected_po_id,
        "source_po_item_id": source_po_item_id,
        "proposed_po_id": f"ALT-{stable_suffix}",
        "original_supplier_id": original_supplier_id,
        "alternative_supplier_id": alternative_supplier_id,
        "alternative_supplier_product_id": alternative_supplier_product_id,
        "product_id": product_id,
        "qty": int(source["qty"]),
        "unit_price": unit_price,
        "currency": "TWD",
        "order_date": date.today().isoformat(),
        "proposed_status": "待入庫",
        "reason": reason,
        "estimated_delay_days": estimated_delay_days,
        "source_event_id": source_event_id,
        "source_po_version": source_version,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    values["proposal_digest"] = _canonical_digest(values)
    return PurchaseProposal(**values)


def proposal_to_execution_request(
    proposal: PurchaseProposal,
) -> PurchaseOrderExecutionRequest:
    """Map proposal payload to the unchanged create-PO Gateway contract."""
    _assert_proposal_integrity(proposal)
    args = {
        "po_id": proposal.proposed_po_id,
        "supplier_id": proposal.alternative_supplier_id,
        "product_id": proposal.product_id,
        "qty": proposal.qty,
        "unit_price": proposal.unit_price,
        "order_date": proposal.order_date,
        "status": proposal.proposed_status,
        "note": (
            f"替代採購提案 {proposal.proposal_id}；"
            f"受影響採購單 {proposal.affected_po_id}"
        ),
        "proposal_id": proposal.proposal_id,
        "proposal_digest": proposal.proposal_digest,
        "affected_po_id": proposal.affected_po_id,
        "source_po_item_id": proposal.source_po_item_id,
        "alternative_supplier_product_id": (
            proposal.alternative_supplier_product_id
        ),
        "source_po_version": proposal.source_po_version,
    }
    return PurchaseOrderExecutionRequest(
        tool_name="create_purchase_order",
        operation_id=proposal_operation_id(proposal.proposal_id),
        contract_version=EXECUTION_CONTRACT_VERSION,
        args=MappingProxyType(args),
    )


def _validate_current_proposal(
    conn: sqlite3.Connection, proposal: PurchaseProposal
) -> None:
    _assert_proposal_integrity(proposal)
    source = _source_po_snapshot(
        conn,
        proposal.affected_po_id,
        proposal.product_id,
        proposal.source_po_item_id,
    )
    current_source = "sha256:" + _canonical_digest(source)
    if not hmac.compare_digest(proposal.source_po_version, current_source):
        raise PermissionError("受影響採購單已變更，請重新建立提案。")
    if not hmac.compare_digest(
        proposal.original_supplier_id, str(source["supplier_id"])
    ):
        raise PermissionError("原供應商與受影響採購單不一致。")
    if isinstance(proposal.qty, bool) or proposal.qty != int(source["qty"]):
        raise PermissionError("提案數量與受影響採購明細不一致。")
    expected_po_id = (
        "ALT-" + hashlib.sha256(proposal.proposal_id.encode("utf-8")).hexdigest()[:16]
    )
    if not hmac.compare_digest(proposal.proposed_po_id, expected_po_id):
        raise PermissionError("替代採購單編號不符合伺服器命名規則。")
    if hmac.compare_digest(
        proposal.original_supplier_id, proposal.alternative_supplier_id
    ):
        raise PermissionError("替代供應商必須與原供應商不同。")
    if proposal.currency != "TWD":
        raise PermissionError("替代採購提案幣別必須為 TWD。")
    if proposal.proposed_status != "待入庫":
        raise PermissionError("替代採購提案狀態必須為待入庫。")
    if not str(proposal.reason or "").strip() or len(proposal.reason) > 1000:
        raise PermissionError("提案理由格式不合法。")
    if proposal.estimated_delay_days is not None and (
        isinstance(proposal.estimated_delay_days, bool)
        or not 0 <= proposal.estimated_delay_days <= 3650
    ):
        raise PermissionError("預估延誤天數超出允許範圍。")
    if proposal.source_event_id is not None and conn.execute(
        "SELECT 1 FROM supply_chain_events WHERE id = ?",
        (proposal.source_event_id,),
    ).fetchone() is None:
        raise PermissionError("找不到來源風險事件。")
    supplier = conn.execute(
        """
        SELECT s.is_official, sp.price
        FROM suppliers s
        JOIN supplier_products sp ON sp.supplier_id = s.supplier_id
        WHERE s.supplier_id = ? AND sp.product_id = ? AND sp.id = ?
        """,
        (
            proposal.alternative_supplier_id,
            proposal.product_id,
            proposal.alternative_supplier_product_id,
        ),
    ).fetchone()
    if supplier is None or int(supplier[0] or 0) != 1:
        raise PermissionError("替代供應商不是有效的正式供應商。")
    if float(supplier[1]) != proposal.unit_price:
        raise PermissionError("替代供應商報價已變更，請重新建立提案。")
    claimed = conn.execute(
        "SELECT proposal_id FROM purchase_proposal_effects "
        "WHERE source_po_item_id = ?",
        (proposal.source_po_item_id,),
    ).fetchone()
    if claimed is not None and not hmac.compare_digest(
        str(claimed[0]), proposal.proposal_id
    ):
        raise PermissionError("此來源採購明細已有另一份替代提案完成執行。")


def _persist_proposal(proposal: PurchaseProposal) -> PurchaseProposal:
    with database.transaction(immediate=True) as conn:
        _validate_current_proposal(conn, proposal)
        existing = _load_proposal_with_conn(conn, proposal.proposal_id)
        if existing is not None:
            _assert_proposal_integrity(existing)
            if not hmac.compare_digest(
                existing.proposal_digest, proposal.proposal_digest
            ):
                raise ValueError("proposal_id 已綁定另一份採購提案。")
            return existing
        try:
            proposed_order_date = date.fromisoformat(proposal.order_date)
            datetime.strptime(proposal.created_at, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError) as exc:
            raise PermissionError("提案日期或建立時間格式不合法。") from exc
        if proposed_order_date != date.today():
            raise PermissionError("新提案的採購日期必須為今日。")
        placeholders = ", ".join("?" for _ in _PROPOSAL_COLUMNS)
        conn.execute(
            f"INSERT INTO purchase_proposals ({', '.join(_PROPOSAL_COLUMNS)}) VALUES ({placeholders})",
            tuple(getattr(proposal, column) for column in _PROPOSAL_COLUMNS),
        )
    return proposal


def submit_purchase_proposal(proposal: PurchaseProposal, *, actor: str):
    """Persist a proposal, then submit its execution request for approval."""
    require_capability(actor, ERP_EXCHANGE_PROPOSE)
    principal = load_principal(actor)
    if principal is None or not hmac.compare_digest(
        principal.username, proposal.proposer_username
    ):
        raise PermissionError("只有原提案人可以送出此提案。")
    if principal.role != proposal.proposer_role:
        raise PermissionError("提案人的角色已變更，請重新建立提案。")
    if not hmac.compare_digest(
        principal.organization_id, proposal.organization_id
    ):
        raise PermissionError("提案人的組織已變更，請重新建立提案。")
    durable = _persist_proposal(proposal)
    request = proposal_to_execution_request(durable)

    from backend.tool_gateway import gateway

    return gateway.call(
        request.tool_name,
        dict(request.args),
        role=principal.role,
        actor=principal.username,
        agent_name="procurement_agent",
        operation_id=request.operation_id,
    )


def validate_purchase_proposal_execution(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    proposal_id: str,
    execution_args: Mapping[str, object],
    requester_username: str | None = None,
) -> PurchaseProposal:
    """Verify immutable proposal evidence and live source preconditions."""
    proposal = _load_proposal_with_conn(conn, proposal_id)
    if proposal is None:
        raise PermissionError("找不到採購提案。")
    _assert_proposal_integrity(proposal)
    if requester_username is not None:
        requester = load_principal(requester_username, conn=conn)
        if requester is None or not requester.can(ERP_EXCHANGE_PROPOSE):
            raise PermissionError("提案人的即時權限已失效。")
        if not hmac.compare_digest(
            requester.username, proposal.proposer_username
        ) or not hmac.compare_digest(
            requester.organization_id, proposal.organization_id
        ):
            raise PermissionError("目前提案人不是此 Proposal 的擁有者。")
    if not hmac.compare_digest(
        proposal_operation_id(proposal.proposal_id), str(operation_id or "")
    ):
        raise PermissionError("執行操作與採購提案不一致。")
    expected = proposal_to_execution_request(proposal).args
    if set(execution_args) != set(expected):
        raise PermissionError("執行請求欄位集合與提案不一致。")
    for field in expected:
        if execution_args.get(field) != expected.get(field):
            raise PermissionError(f"執行請求欄位 {field} 與提案不一致。")
    _validate_current_proposal(conn, proposal)
    return proposal


def validate_purchase_proposal_gateway_request(
    *,
    operation_id: str,
    proposal_id: str,
    execution_args: Mapping[str, object],
    actor: str,
) -> None:
    """Reload and verify a planner's canonical Proposal at the Gateway boundary."""
    with database.transaction() as conn:
        validate_purchase_proposal_execution(
            conn,
            operation_id=operation_id,
            proposal_id=proposal_id,
            execution_args=execution_args,
            requester_username=actor,
        )


def validate_purchase_proposal_decision_scope(
    conn: sqlite3.Connection, *, proposal_id: str, actor: str
) -> PurchaseProposal:
    """Enforce Proposal organization scope inside a Gateway decision transaction."""
    principal = load_principal(actor, conn=conn)
    if principal is None or not principal.can(APPROVAL_DECIDE):
        raise PermissionError("決策者目前不具採購核准權限。")
    proposal = _load_proposal_with_conn(conn, proposal_id)
    if proposal is None:
        raise PermissionError("找不到採購提案。")
    _assert_proposal_integrity(proposal)
    if not hmac.compare_digest(
        principal.organization_id, proposal.organization_id
    ):
        raise PermissionError("不可處理其他組織的採購提案。")
    return proposal


def get_purchase_proposal_evidence(
    proposal_id: str, *, actor: str
) -> PurchaseProposal | None:
    principal = load_principal(actor)
    if principal is None:
        raise PermissionError("讀取提案證據需要有效身分。")
    with database.transaction() as conn:
        proposal = _load_proposal_with_conn(conn, proposal_id)
        if proposal is None:
            return None
        if not hmac.compare_digest(
            principal.organization_id, proposal.organization_id
        ):
            raise PermissionError("不可讀取其他組織的採購提案。")
        can_review = principal.can(PROPOSAL_EVIDENCE_READ)
        owns_proposal = principal.can(ERP_EXCHANGE_PROPOSE) and hmac.compare_digest(
            principal.username, proposal.proposer_username
        )
        if not (can_review or owns_proposal):
            raise PermissionError("沒有讀取此提案證據的權限。")
        _assert_proposal_integrity(proposal)
        return proposal


def get_purchase_proposal_for_operation(
    operation_id: str | None, *, actor: str
) -> PurchaseProposal | None:
    """Resolve proposal evidence for a Gateway operation; legacy rows return None."""
    if not operation_id:
        return None
    try:
        proposal_id = _proposal_id_from_operation_id(operation_id)
    except ValueError:
        return None
    return get_purchase_proposal_evidence(proposal_id, actor=actor)


def _load_bound_approval(proposal: PurchaseProposal) -> dict | None:
    rows = database.run_query(
        """
        SELECT approval_id, tool_name, parameters, requester_username, status,
               approver, created_at, updated_at, reason
        FROM pending_approvals WHERE operation_id = ?
        """,
        (proposal_operation_id(proposal.proposal_id),),
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "approval_id": row[0],
        "tool_name": row[1],
        "parameters": row[2],
        "requester_username": row[3],
        "status": row[4],
        "approver": row[5],
        "created_at": row[6],
        "updated_at": row[7],
        "reason": row[8],
    }


def decide_purchase_proposal(decision: ApprovalDecision, *, actor: str):
    """Apply an L3 decision without placing PO args inside the decision DTO."""
    require_capability(actor, APPROVAL_DECIDE)
    proposal = get_purchase_proposal_evidence(decision.proposal_id, actor=actor)
    if proposal is None:
        raise ValueError("找不到採購提案。")
    approval = _load_bound_approval(proposal)
    if approval is None:
        raise ValueError("採購提案尚未送交 Gateway 審批。")
    execution = proposal_to_execution_request(proposal)
    try:
        approval_args = json.loads(approval["parameters"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise PermissionError("審批執行內容已損壞。") from exc
    if (
        approval["tool_name"] != execution.tool_name
        or approval["requester_username"] != proposal.proposer_username
        or approval_args != dict(execution.args)
    ):
        raise PermissionError("審批內容與採購提案不一致。")

    outcome = str(decision.outcome or "").strip().lower()
    from backend.tool_gateway import gateway

    if outcome == "approve":
        return gateway.approve_action(approval["approval_id"], approver=actor)
    if outcome == "reject":
        reason = _required_text(decision.reason, "拒絕原因")
        return gateway.reject_action(
            approval["approval_id"], reason, approver=actor
        )
    raise ValueError("outcome 必須是 approve 或 reject。")


def get_purchase_operation_timeline(operation_id: str, *, actor: str) -> list[dict]:
    """Return a redacted, correlation-based L3 audit timeline."""
    require_capability(actor, PROPOSAL_EVIDENCE_READ)
    proposal_id = _proposal_id_from_operation_id(operation_id)
    proposal = get_purchase_proposal_evidence(proposal_id, actor=actor)
    if proposal is None:
        return []
    approval = _load_bound_approval(proposal)
    receipts = database.run_query(
        "SELECT receipt_id, approval_id, created_at FROM effect_receipts WHERE operation_id = ?",
        (operation_id,),
    )

    events = [
        {
            "kind": "proposal_created",
            "operation_id": operation_id,
            "time": proposal.created_at,
            "actor": proposal.proposer_username,
            "proposal_id": proposal.proposal_id,
            "summary": (
                f"{proposal.affected_po_id} -> {proposal.proposed_po_id}; "
                f"{proposal.original_supplier_id} -> {proposal.alternative_supplier_id}"
            ),
        }
    ]
    if approval is not None:
        events.append(
            {
                "kind": "approval_submitted",
                "operation_id": operation_id,
                "time": approval["created_at"],
                "actor": approval["requester_username"],
                "approval_id": approval["approval_id"],
                "summary": f"approval status: {approval['status']}",
            }
        )
        if approval["status"] == "rejected":
            events.append(
                {
                    "kind": "approval_rejected",
                    "operation_id": operation_id,
                    "time": approval["updated_at"],
                    "actor": approval["approver"],
                    "approval_id": approval["approval_id"],
                    "summary": "Proposal rejected by an authorized reviewer.",
                }
            )
    if receipts:
        receipt = receipts[0]
        events.append(
            {
                "kind": "execution_completed",
                "operation_id": operation_id,
                "time": receipt[2],
                "actor": approval["approver"] if approval else None,
                "approval_id": receipt[1],
                "receipt_id": receipt[0],
                "summary": "Gateway committed one ERP effect and receipt.",
            }
        )
    return events


def list_impacted_purchase_options(*, actor: str) -> list[dict]:
    """Return structured open PO lines for the L2 proposal workbench."""
    require_capability(actor, ERP_EXCHANGE_PROPOSE)
    with database.transaction() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT i.id AS source_po_item_id,
                   p.po_id, p.supplier_id AS original_supplier_id,
                   s.name AS supplier_name, s.country, s.region,
                   p.estimated_delay_days, p.alternative_suggestion,
                   i.product_id, inv.name AS product_name, i.qty, i.unit_price
            FROM purchase_orders p
            JOIN suppliers s ON s.supplier_id = p.supplier_id
            JOIN purchase_order_items i ON i.po_id = p.po_id
            LEFT JOIN inventory inv ON inv.product_id = i.product_id
            WHERE (p.status IS NULL OR p.status NOT IN ('已完成', '已取消'))
              AND (
                    p.estimated_delay_days IS NOT NULL
                 OR TRIM(COALESCE(p.alternative_suggestion, '')) <> ''
              )
            ORDER BY COALESCE(p.estimated_delay_days, 0) DESC, p.po_id, i.id
            """
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["source_po_version"] = source_po_version(
                conn,
                item["po_id"],
                item["product_id"],
                item["source_po_item_id"],
            )
            result.append(item)
        return result


def list_alternative_suppliers(
    *,
    affected_po_id: str,
    product_id: str,
    actor: str,
    source_po_item_id: int | None = None,
) -> list[dict]:
    """Return active supplier-master candidates for one affected PO line."""
    require_capability(actor, ERP_EXCHANGE_PROPOSE)
    with database.transaction() as conn:
        conn.row_factory = sqlite3.Row
        source = _source_po_snapshot(
            conn, affected_po_id, product_id, source_po_item_id
        )
        rows = conn.execute(
            """
            SELECT sp.id AS supplier_product_id,
                   s.supplier_id, s.name, s.country, s.region, s.risk_level,
                   sp.price, sp.carbon_factor
            FROM suppliers s
            JOIN supplier_products sp ON sp.supplier_id = s.supplier_id
            WHERE sp.product_id = ? AND s.is_official = 1
              AND s.supplier_id <> ?
            ORDER BY COALESCE(s.risk_level, ''), sp.price, s.supplier_id
            """,
            (product_id, source["supplier_id"]),
        ).fetchall()
        return [dict(row) for row in rows]
