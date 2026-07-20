"""Validated CSV batch exchange for single-item external purchase orders.

V1 deliberately uses a fixed schema: one CSV row represents one purchase order
with exactly one item. Validated rows are staged separately from live ERP data;
only the Tool Gateway's approved transaction may insert or update a live PO.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import io
import json
import math
import os
import re
import sqlite3
from typing import Iterable

from . import database
from .access_control import (
    ERP_EXCHANGE_EXPORT,
    ERP_EXCHANGE_PROPOSE,
    ERP_EXCHANGE_RECONCILE,
    PROPOSAL_EVIDENCE_READ,
    require_any_capability,
    require_capability,
)


MAX_IMPORT_BYTES = 1_000_000
MAX_IMPORT_ROWS = 500
IMPORT_COLUMNS = (
    "external_id",
    "po_id",
    "supplier_id",
    "product_id",
    "qty",
    "unit_price",
    "order_date",
    "status",
    "note",
)
RECEIPT_COLUMNS = (
    "source_system",
    "external_id",
    "operation_id",
    "approval_id",
    "payload_digest",
    "receipt_attempt_id",
    "receipt_status",
    "message",
    "key_id",
    "signature",
)
EXPORT_COLUMNS = (
    "source_system",
    "external_id",
    "operation_id",
    "approval_id",
    "payload_digest",
    "version",
    "po_id",
    "supplier_id",
    "product_id",
    "qty",
    "unit_price",
    "order_date",
    "status",
    "note",
)
ERP_EXCHANGE_POLICY_VERSION = "external-po-sync-v2"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_ERP_EXCHANGE_APPROVAL_CONTEXT = object()


def _clean_text(value, field_name: str, *, required: bool, max_length: int) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field_name} 不可空白")
    if len(value) > max_length:
        raise ValueError(f"{field_name} 長度超過 {max_length}")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in value):
        raise ValueError(f"{field_name} 含不允許的控制字元")
    return value


def _identifier(value, field_name: str) -> str:
    value = _clean_text(value, field_name, required=True, max_length=128)
    if value.startswith(_FORMULA_PREFIXES) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field_name} 格式不合法")
    return value


def normalize_source_system(value: str) -> str:
    value = _clean_text(value, "source_system", required=True, max_length=64)
    value = value.lower()
    if value.startswith(_FORMULA_PREFIXES) or not _SOURCE_RE.fullmatch(value):
        raise ValueError("source_system 格式不合法")
    return value


def _normalize_row(row: dict) -> dict:
    if not isinstance(row, dict):
        raise ValueError("CSV 資料列格式不合法")

    external_id = _identifier(row.get("external_id"), "external_id")
    po_id = _identifier(row.get("po_id"), "po_id")
    supplier_id = _identifier(row.get("supplier_id"), "supplier_id")
    product_id = _identifier(row.get("product_id"), "product_id")

    qty_raw = str(row.get("qty", "")).strip()
    if not qty_raw.isdigit():
        raise ValueError("qty 必須是正整數")
    qty = int(qty_raw)
    if qty <= 0 or qty > 1_000_000_000:
        raise ValueError("qty 必須是合理範圍內的正整數")

    price_raw = str(row.get("unit_price", "")).strip()
    try:
        price_decimal = Decimal(price_raw)
    except (InvalidOperation, ValueError):
        raise ValueError("unit_price 必須是非負有限數字") from None
    if not price_decimal.is_finite() or price_decimal < 0 or price_decimal > Decimal("1e12"):
        raise ValueError("unit_price 必須是非負有限數字")
    unit_price = float(price_decimal)

    order_date_raw = _clean_text(
        row.get("order_date"), "order_date", required=True, max_length=10
    )
    try:
        order_date = date.fromisoformat(order_date_raw).isoformat()
    except ValueError:
        raise ValueError("order_date 必須是 YYYY-MM-DD") from None

    status = _clean_text(row.get("status"), "status", required=True, max_length=40)
    note = _clean_text(row.get("note"), "note", required=False, max_length=500)
    return {
        "external_id": external_id,
        "po_id": po_id,
        "supplier_id": supplier_id,
        "product_id": product_id,
        "qty": qty,
        "unit_price": unit_price,
        "order_date": order_date,
        "status": status,
        "note": note,
    }


def parse_purchase_order_csv(content: bytes) -> list[dict]:
    """Parse a strict UTF-8 CSV without writing any business data."""
    if not isinstance(content, (bytes, bytearray)):
        raise ValueError("CSV 內容必須是 bytes")
    if len(content) > MAX_IMPORT_BYTES:
        raise ValueError(f"CSV 檔案大小不可超過 {MAX_IMPORT_BYTES} bytes")
    if not content:
        raise ValueError("CSV 檔案不可為空")
    try:
        text = bytes(content).decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise ValueError("CSV 必須使用 UTF-8 編碼") from None
    if "\x00" in text:
        raise ValueError("CSV 含不允許的 NUL 字元")

    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)) or set(headers) != set(IMPORT_COLUMNS):
        missing = sorted(set(IMPORT_COLUMNS) - set(headers))
        unknown = sorted(set(headers) - set(IMPORT_COLUMNS))
        raise ValueError(f"CSV 欄位不符；缺少={missing}，未知={unknown}")

    rows: list[dict] = []
    seen_external_ids: set[str] = set()
    for line_number, raw_row in enumerate(reader, start=2):
        if len(rows) >= MAX_IMPORT_ROWS:
            raise ValueError(f"CSV 筆數不可超過 {MAX_IMPORT_ROWS}")
        if None in raw_row:
            raise ValueError(f"第 {line_number} 列欄位數量不符")
        try:
            row = _normalize_row(raw_row)
        except ValueError as exc:
            raise ValueError(f"第 {line_number} 列：{exc}") from None
        if row["external_id"] in seen_external_ids:
            raise ValueError(f"CSV 內重複 external_id：{row['external_id']}")
        seen_external_ids.add(row["external_id"])
        rows.append(row)
    if not rows:
        raise ValueError("CSV 沒有資料列")
    return rows


def _record_digest(row: dict) -> str:
    canonical = json.dumps(
        {column: row[column] for column in IMPORT_COLUMNS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_known_ids(conn, table: str, id_column: str, ids: set[str]) -> dict:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE {id_column} IN ({placeholders})", tuple(ids)
    ).fetchall()
    return {row[id_column]: row for row in rows}


def stage_purchase_order_rows(
    source_system: str, rows: Iterable[dict], *, actor: str
) -> dict:
    """Atomically stage a fully valid batch; invalid batches write nothing."""
    source_system = normalize_source_system(source_system)
    normalized = [_normalize_row(row) for row in rows]
    if not normalized:
        raise ValueError("批次沒有資料列")
    if len(normalized) > MAX_IMPORT_ROWS:
        raise ValueError(f"批次筆數不可超過 {MAX_IMPORT_ROWS}")
    external_ids = [row["external_id"] for row in normalized]
    if len(external_ids) != len(set(external_ids)):
        raise ValueError("批次內有重複 external_id")
    po_ids = [row["po_id"] for row in normalized]
    if len(po_ids) != len(set(po_ids)):
        raise ValueError("批次內有重複 po_id")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = {"inserted": 0, "updated": 0, "unchanged": 0, "records": []}
    with database.transaction(immediate=True) as conn:
        conn.row_factory = sqlite3.Row
        _require_exchange_actor(actor, conn, ERP_EXCHANGE_PROPOSE)
        suppliers = _load_known_ids(
            conn, "suppliers", "supplier_id", {row["supplier_id"] for row in normalized}
        )
        products = _load_known_ids(
            conn, "inventory", "product_id", {row["product_id"] for row in normalized}
        )
        for row in normalized:
            supplier = suppliers.get(row["supplier_id"])
            if supplier is None:
                raise ValueError(f"未知 supplier_id：{row['supplier_id']}")
            if int(supplier["is_official"] or 0) != 1:
                raise ValueError(f"供應商不是正式供應商：{row['supplier_id']}")
            if row["product_id"] not in products:
                raise ValueError(f"未知 product_id：{row['product_id']}")

        for row in normalized:
            digest = _record_digest(row)
            po_owner = conn.execute(
                """
                SELECT external_id FROM erp_exchange_records
                WHERE source_system = ? AND po_id = ? AND external_id <> ?
                """,
                (source_system, row["po_id"], row["external_id"]),
            ).fetchone()
            if po_owner is not None:
                raise ValueError(
                    f"po_id 已綁定其他 external_id：{row['po_id']}"
                )
            existing = conn.execute(
                "SELECT * FROM erp_exchange_records "
                "WHERE source_system = ? AND external_id = ?",
                (source_system, row["external_id"]),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO erp_exchange_records (
                        source_system, external_id, po_id, supplier_id,
                        product_id, qty, unit_price, order_date, status, note,
                        content_digest, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        source_system,
                        row["external_id"],
                        row["po_id"],
                        row["supplier_id"],
                        row["product_id"],
                        row["qty"],
                        row["unit_price"],
                        row["order_date"],
                        row["status"],
                        row["note"],
                        digest,
                        now,
                        now,
                    ),
                )
                version = 1
                summary["inserted"] += 1
            elif existing["content_digest"] == digest:
                version = int(existing["version"])
                summary["unchanged"] += 1
            else:
                if (
                    existing["last_synced_version"] is not None
                    and existing["po_id"] != row["po_id"]
                ):
                    raise ValueError("已同步的 external_id 不可改變 po_id")
                version = int(existing["version"]) + 1
                conn.execute(
                    """
                    UPDATE erp_exchange_records
                    SET po_id = ?, supplier_id = ?, product_id = ?, qty = ?,
                        unit_price = ?, order_date = ?, status = ?, note = ?,
                        content_digest = ?, version = ?, updated_at = ?
                    WHERE source_system = ? AND external_id = ?
                    """,
                    (
                        row["po_id"],
                        row["supplier_id"],
                        row["product_id"],
                        row["qty"],
                        row["unit_price"],
                        row["order_date"],
                        row["status"],
                        row["note"],
                        digest,
                        version,
                        now,
                        source_system,
                        row["external_id"],
                    ),
                )
                summary["updated"] += 1
            summary["records"].append(
                {
                    "source_system": source_system,
                    "external_id": row["external_id"],
                    "version": version,
                    "content_digest": digest,
                }
            )
    return summary


def _dict_row(row: sqlite3.Row | tuple, columns: list[str] | None = None) -> dict:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if columns is None:
        raise ValueError("columns are required for tuple rows")
    return dict(zip(columns, row))


def exchange_resource_version(record: dict) -> str:
    stored_digest = str(record.get("content_digest") or "")
    actual_digest = _record_digest(record)
    if not stored_digest or not hmac.compare_digest(stored_digest, actual_digest):
        raise ValueError("ERP 交換暫存資料完整性驗證失敗，疑似遭竄改")
    base_version = record.get("last_synced_version")
    base = "absent" if base_version is None else str(int(base_version))
    return f"v{int(record['version'])}:{stored_digest}:base-{base}"


def build_exchange_operation_id(
    source_system: str, external_id: str, version: int
) -> str:
    source_system = normalize_source_system(source_system)
    external_id = _identifier(external_id, "external_id")
    if isinstance(version, bool) or int(version) <= 0:
        raise ValueError("version 必須是正整數")
    identity = json.dumps(
        [source_system, external_id, int(version)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "erp-csv-sync:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def get_exchange_record(
    source_system: str, external_id: str, *, conn=None
) -> dict | None:
    source_system = normalize_source_system(source_system)
    external_id = _identifier(external_id, "external_id")
    query = """
        SELECT e.*, s.country AS supplier_country,
               s.region AS supplier_region, s.risk_level AS supplier_risk_level
        FROM erp_exchange_records e
        LEFT JOIN suppliers s ON s.supplier_id = e.supplier_id
        WHERE e.source_system = ? AND e.external_id = ?
    """
    if conn is not None:
        old_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(query, (source_system, external_id)).fetchone()
        finally:
            conn.row_factory = old_factory
    else:
        with sqlite3.connect(database.DB_FILE) as owned:
            owned.row_factory = sqlite3.Row
            row = owned.execute(query, (source_system, external_id)).fetchone()
    if row is None:
        return None
    record = dict(row)
    return _with_sync_state(record, conn=conn)


def _with_sync_state(record: dict, *, conn=None) -> dict:
    operation_id = build_exchange_operation_id(
        record["source_system"], record["external_id"], record["version"]
    )
    query = """
        SELECT p.approval_id, p.status, p.payload_digest,
               r.operation_id AS receipt_operation_id,
               r.source_system AS receipt_source_system,
               r.external_id AS receipt_external_id,
               r.approval_id AS receipt_approval_id,
               r.payload_digest AS receipt_payload_digest,
               r.receipt_attempt_id, r.receipt_status, r.message,
               r.key_id, r.signature, r.received_by,
               CASE
                   WHEN r.receipt_attempt_id IS NOT NULL
                    AND r.key_id IS NOT NULL
                    AND r.signature IS NOT NULL
                    AND r.received_by IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                        FROM erp_exchange_receipt_events e
                        WHERE e.receipt_attempt_id = r.receipt_attempt_id
                          AND e.operation_id = r.operation_id
                          AND e.source_system = r.source_system
                          AND e.external_id = r.external_id
                          AND e.approval_id = r.approval_id
                          AND e.payload_digest = r.payload_digest
                          AND e.receipt_status = r.receipt_status
                          AND e.key_id = r.key_id
                          AND e.signature = r.signature
                          AND e.received_by = r.received_by
                    )
                   THEN 1 ELSE 0
               END AS receipt_event_matched
        FROM pending_approvals p
        LEFT JOIN erp_exchange_receipts r ON r.operation_id = p.operation_id
        WHERE p.operation_id = ?
    """
    if conn is not None:
        old_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            approval = conn.execute(query, (operation_id,)).fetchone()
        finally:
            conn.row_factory = old_factory
    else:
        with sqlite3.connect(database.DB_FILE) as owned:
            owned.row_factory = sqlite3.Row
            approval = owned.execute(query, (operation_id,)).fetchone()
    record["current_operation_id"] = operation_id
    record["current_approval_id"] = approval["approval_id"] if approval else None
    record["approval_status"] = approval["status"] if approval else None
    record["receipt_status"] = approval["receipt_status"] if approval else None
    receipt_payload = None
    if approval and approval["receipt_status"]:
        receipt_payload = {
            "source_system": approval["receipt_source_system"],
            "external_id": approval["receipt_external_id"],
            "operation_id": approval["receipt_operation_id"],
            "approval_id": approval["receipt_approval_id"],
            "payload_digest": approval["receipt_payload_digest"],
            "receipt_attempt_id": approval["receipt_attempt_id"],
            "receipt_status": approval["receipt_status"],
            "message": approval["message"],
            "key_id": approval["key_id"],
            "signature": approval["signature"],
        }
    record["receipt_verified"] = _receipt_is_verified(
        receipt_payload,
        event_matched=bool(approval and approval["receipt_event_matched"]),
    )
    if approval and approval["receipt_status"] and not record["receipt_verified"]:
        record["sync_state"] = "unverified_legacy"
    elif approval and approval["receipt_status"] == "accepted":
        record["sync_state"] = "acknowledged"
    elif approval and approval["receipt_status"] == "rejected":
        record["sync_state"] = "receipt_rejected"
    elif approval and approval["receipt_status"] == "error":
        record["sync_state"] = "receipt_error"
    elif approval and approval["status"] == "approved":
        record["sync_state"] = "approved"
    elif approval and approval["status"] == "pending":
        record["sync_state"] = "pending"
    elif approval and approval["status"] == "executing":
        record["sync_state"] = "executing"
    elif approval and approval["status"] == "rejected":
        record["sync_state"] = "rejected"
    elif record.get("last_synced_version") == record.get("version"):
        record["sync_state"] = "approved"
    else:
        record["sync_state"] = "staged"
    return record


def list_exchange_records(
    source_system: str | None = None, *, actor: str
) -> list[dict]:
    params: tuple = ()
    where = ""
    if source_system is not None:
        source_system = normalize_source_system(source_system)
        where = "WHERE e.source_system = ?"
        params = (source_system,)
    with sqlite3.connect(database.DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        actor = _clean_text(actor, "actor", required=True, max_length=128)
        require_any_capability(
            actor,
            {ERP_EXCHANGE_PROPOSE, PROPOSAL_EVIDENCE_READ},
            conn=conn,
        )
        rows = conn.execute(
            f"""
            SELECT e.*, s.country AS supplier_country,
                   s.region AS supplier_region, s.risk_level AS supplier_risk_level
            FROM erp_exchange_records e
            LEFT JOIN suppliers s ON s.supplier_id = e.supplier_id
            {where}
            ORDER BY e.updated_at DESC, e.external_id
            """,
            params,
        ).fetchall()
        return [_with_sync_state(dict(row), conn=conn) for row in rows]


def sync_external_purchase_order(
    source_system: str, external_id: str, **_internal
) -> dict:
    """Apply one approved staged revision inside the Gateway transaction."""
    conn = _internal.get("_conn")
    operation_id = _internal.get("_operation_id")
    expected_resource_version = _internal.get("_resource_version")
    approval_context = _internal.get("_approval_context")
    if (
        approval_context is not _ERP_EXCHANGE_APPROVAL_CONTEXT
        or not isinstance(conn, sqlite3.Connection)
        or not conn.in_transaction
        or not isinstance(operation_id, str)
        or not operation_id
        or not isinstance(expected_resource_version, str)
    ):
        raise PermissionError(
            "sync_external_purchase_order requires an approved Gateway transaction"
        )

    source_system = normalize_source_system(source_system)
    external_id = _identifier(external_id, "external_id")
    approval = conn.execute(
        """
        SELECT 1 FROM pending_approvals
        WHERE operation_id = ?
          AND tool_name = 'sync_external_purchase_order'
          AND status = 'executing'
        LIMIT 1
        """,
        (operation_id,),
    ).fetchone()
    if approval is None:
        raise PermissionError("external sync requires a matching executing approval")

    record = get_exchange_record(source_system, external_id, conn=conn)
    if record is None:
        raise ValueError("找不到待同步的 ERP 交換資料")
    current_resource_version = exchange_resource_version(record)
    if current_resource_version != expected_resource_version:
        raise ValueError("ERP 交換資料版本已失效，請重新送審")
    expected_operation_id = build_exchange_operation_id(
        source_system, external_id, record["version"]
    )
    if operation_id != expected_operation_id:
        raise ValueError("operation_id 與 ERP 交換資料版本不相符")

    supplier = conn.execute(
        "SELECT is_official FROM suppliers WHERE supplier_id = ?",
        (record["supplier_id"],),
    ).fetchone()
    if supplier is None:
        raise ValueError(f"unknown supplier_id: {record['supplier_id']}")
    if int(supplier[0] or 0) != 1:
        raise PermissionError(f"supplier is not official: {record['supplier_id']}")
    if conn.execute(
        "SELECT 1 FROM inventory WHERE product_id = ?", (record["product_id"],)
    ).fetchone() is None:
        raise ValueError(f"unknown product_id: {record['product_id']}")
    if not math.isfinite(float(record["unit_price"])) or record["unit_price"] < 0:
        raise ValueError("unit_price must be a non-negative finite number")
    if int(record["qty"]) <= 0:
        raise ValueError("qty must be a positive integer")

    total_amount = int(record["qty"]) * float(record["unit_price"])
    existing = conn.execute(
        """
        SELECT po_id, external_version FROM purchase_orders
        WHERE external_source_system = ? AND external_id = ?
        """,
        (source_system, external_id),
    ).fetchone()
    if existing is None:
        if record["last_synced_version"] is not None:
            raise ValueError("先前已同步的本機採購單不存在，資源版本已失效")
        collision = conn.execute(
            "SELECT external_source_system, external_id FROM purchase_orders WHERE po_id = ?",
            (record["po_id"],),
        ).fetchone()
        if collision is not None:
            raise ValueError("po_id 已被其他採購單使用，拒絕外部資料綁定")
        conn.execute(
            """
            INSERT INTO purchase_orders (
                po_id, supplier_id, order_date, status, total_amount, note,
                operation_id, last_sync_operation_id, external_source_system,
                external_id, external_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["po_id"],
                record["supplier_id"],
                record["order_date"],
                record["status"],
                total_amount,
                record["note"],
                operation_id,
                operation_id,
                source_system,
                external_id,
                record["version"],
            ),
        )
        effect = "inserted"
    else:
        if existing[0] != record["po_id"]:
            raise ValueError("外部資料已綁定不同 po_id")
        if existing[1] != record["last_synced_version"]:
            raise ValueError("本機採購單同步版本已失效，請重新檢查資料")
        cursor = conn.execute(
            """
            UPDATE purchase_orders
            SET supplier_id = ?, order_date = ?, status = ?, total_amount = ?,
                note = ?, last_sync_operation_id = ?, external_version = ?
            WHERE external_source_system = ? AND external_id = ?
              AND external_version = ?
            """,
            (
                record["supplier_id"],
                record["order_date"],
                record["status"],
                total_amount,
                record["note"],
                operation_id,
                record["version"],
                source_system,
                external_id,
                record["last_synced_version"],
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("採購單同步版本競態，更新未生效")
        conn.execute("DELETE FROM purchase_order_items WHERE po_id = ?", (record["po_id"],))
        effect = "updated"

    conn.execute(
        """
        INSERT INTO purchase_order_items (po_id, product_id, qty, unit_price)
        VALUES (?, ?, ?, ?)
        """,
        (
            record["po_id"],
            record["product_id"],
            record["qty"],
            record["unit_price"],
        ),
    )
    conn.execute(
        """
        UPDATE erp_exchange_records
        SET last_synced_version = ?, last_synced_operation_id = ?, updated_at = ?
        WHERE source_system = ? AND external_id = ? AND version = ?
        """,
        (
            record["version"],
            operation_id,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            source_system,
            external_id,
            record["version"],
        ),
    )
    return {
        "effect": effect,
        "source_system": source_system,
        "external_id": external_id,
        "version": int(record["version"]),
        "po_id": record["po_id"],
        "operation_id": operation_id,
        "supplier_id": record["supplier_id"],
        "product_id": record["product_id"],
        "qty": int(record["qty"]),
        "unit_price": float(record["unit_price"]),
        "total_amount": total_amount,
        "order_date": record["order_date"],
        "status": record["status"],
        "note": record["note"],
    }


def _require_exchange_actor(
    actor: str, conn: sqlite3.Connection, capability: str
) -> str:
    actor = _clean_text(actor, "actor", required=True, max_length=128)
    require_capability(actor, capability, conn=conn)
    return actor


def _receipt_verification_config() -> tuple[str, bytes]:
    key_id = _clean_text(
        os.getenv("ERP_EXCHANGE_RECEIPT_KEY_ID", ""),
        "ERP_EXCHANGE_RECEIPT_KEY_ID",
        required=True,
        max_length=64,
    )
    secret_text = os.getenv("ERP_EXCHANGE_RECEIPT_HMAC_SECRET", "")
    secret = secret_text.encode("utf-8")
    if len(secret) < 32:
        raise RuntimeError(
            "ERP 回執 HMAC 尚未設定，或密鑰長度不足 32 bytes"
        )
    return key_id, secret


def compute_receipt_signature(row: dict, secret: str | bytes) -> str:
    """Return the external connector's HMAC-SHA256 receipt signature."""
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if len(secret_bytes) < 32:
        raise ValueError("ERP receipt HMAC secret must be at least 32 bytes")
    signed_fields = tuple(
        column for column in RECEIPT_COLUMNS if column != "signature"
    )
    payload = {
        field: "" if row.get(field) is None else str(row.get(field))
        for field in signed_fields
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hmac.new(
        secret_bytes, canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _receipt_is_verified(
    receipt: dict | None, *, event_matched: bool
) -> bool:
    """Fail closed unless the current summary matches a valid signed event."""
    if not receipt or not event_matched:
        return False
    try:
        expected_key_id, secret = _receipt_verification_config()
        if receipt.get("key_id") != expected_key_id:
            return False
        submitted_signature = str(receipt.get("signature") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", submitted_signature):
            return False
        expected_signature = compute_receipt_signature(receipt, secret)
    except (TypeError, ValueError, RuntimeError):
        return False
    return hmac.compare_digest(submitted_signature, expected_signature)


def _safe_csv_cell(value) -> str:
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _serialize_csv(columns: tuple[str, ...], rows: Iterable[dict]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _safe_csv_cell(row.get(column)) for column in columns})
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def _write_exchange_access_audit(
    action: str,
    *,
    actor: str,
    source_system: str | None,
    rows: Iterable[dict],
    result: str,
    conn=None,
) -> None:
    """Append a content-minimized, hash-chained access event."""
    operation_ids = sorted(str(row["operation_id"]) for row in rows)
    operation_set_digest = hashlib.sha256(
        json.dumps(
            operation_ids,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    from backend.agent_logger import write_action_log

    write_action_log(
        action,
        {
            "source_system": source_system or "*",
            "row_count": len(operation_ids),
            "operation_set_digest": operation_set_digest,
        },
        actor,
        result,
        True,
        conn=conn,
    )


def build_purchase_order_template_csv() -> bytes:
    return _serialize_csv(IMPORT_COLUMNS, [])


def _validated_action_snapshot(row: sqlite3.Row) -> dict:
    """Validate and return the immutable action saved in an effect receipt."""
    try:
        result = json.loads(row["result"])
    except (TypeError, json.JSONDecodeError):
        raise ValueError("本機執行收據格式損壞，拒絕匯出或對帳") from None
    if not isinstance(result, dict):
        raise ValueError("本機執行收據格式不合法，拒絕匯出或對帳")

    source_system = normalize_source_system(result.get("source_system"))
    normalized = _normalize_row(
        {
            "external_id": result.get("external_id"),
            "po_id": result.get("po_id"),
            "supplier_id": result.get("supplier_id"),
            "product_id": result.get("product_id"),
            "qty": result.get("qty"),
            "unit_price": result.get("unit_price"),
            "order_date": result.get("order_date"),
            "status": result.get("status"),
            "note": result.get("note"),
        }
    )
    try:
        version = int(result.get("version"))
    except (TypeError, ValueError):
        raise ValueError("本機執行收據缺少合法版本") from None
    if version <= 0 or result.get("operation_id") != row["operation_id"]:
        raise ValueError("本機執行收據的 operation 或版本不相符")
    expected_operation_id = build_exchange_operation_id(
        source_system, normalized["external_id"], version
    )
    if not hmac.compare_digest(row["operation_id"], expected_operation_id):
        raise ValueError("本機執行收據的外部身分與 operation 不相符")

    try:
        parameters = json.loads(row["parameters"])
    except (TypeError, json.JSONDecodeError):
        raise ValueError("核准參數格式損壞，拒絕匯出或對帳") from None
    if (
        not isinstance(parameters, dict)
        or set(parameters) != {"source_system", "external_id"}
        or normalize_source_system(parameters.get("source_system")) != source_system
        or _identifier(parameters.get("external_id"), "external_id")
        != normalized["external_id"]
        or row["policy_version"] != ERP_EXCHANGE_POLICY_VERSION
    ):
        raise ValueError("核准參數與本機執行收據不相符")

    snapshot_digest = _record_digest(normalized)
    resource_version = str(row["resource_version"] or "")
    match = re.fullmatch(
        rf"v{version}:([0-9a-f]{{64}}):base-(?:absent|[0-9]+)",
        resource_version,
    )
    if match is None or not hmac.compare_digest(
        match.group(1), snapshot_digest
    ):
        raise ValueError("核准快照完整性驗證失敗，拒絕匯出或對帳")

    from backend.tool_gateway import canonical_payload_digest

    expected_payload_digest = canonical_payload_digest(
        tool_name="sync_external_purchase_order",
        args=parameters,
        resource_version=resource_version,
        policy_version=row["policy_version"],
        requester_username=row["requester_username"],
    )
    if not hmac.compare_digest(row["payload_digest"], expected_payload_digest):
        raise ValueError("核准內容摘要驗證失敗，拒絕匯出或對帳")

    return {
        "source_system": source_system,
        "external_id": normalized["external_id"],
        "operation_id": row["operation_id"],
        "approval_id": row["approval_id"],
        "payload_digest": row["payload_digest"],
        "version": version,
        **{column: normalized[column] for column in IMPORT_COLUMNS if column != "external_id"},
    }


def _approved_action_rows(
    source_system: str | None, *, actor: str
) -> list[dict]:
    source_filter = (
        normalize_source_system(source_system)
        if source_system is not None
        else None
    )
    with sqlite3.connect(database.DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        _require_exchange_actor(actor, conn, ERP_EXCHANGE_EXPORT)
        rows = conn.execute(
            """
            SELECT p.operation_id, p.approval_id, p.payload_digest,
                   p.parameters, p.resource_version, p.policy_version,
                   p.requester_username,
                   local_receipt.result
            FROM pending_approvals p
            JOIN effect_receipts local_receipt
              ON local_receipt.operation_id = p.operation_id
             AND local_receipt.approval_id = p.approval_id
             AND local_receipt.payload_digest = p.payload_digest
            WHERE p.tool_name = 'sync_external_purchase_order'
              AND p.status = 'approved'
            ORDER BY p.created_at, p.operation_id
            """
        ).fetchall()
        actions = [_validated_action_snapshot(row) for row in rows]
    if source_filter is not None:
        actions = [
            action
            for action in actions
            if action["source_system"] == source_filter
        ]
    actions.sort(
        key=lambda item: (
            item["source_system"],
            item["external_id"],
            item["version"],
            item["operation_id"],
        )
    )
    return actions


def export_approved_actions_csv(
    source_system: str | None = None, *, actor: str
) -> bytes:
    """Export immutable, approved execution snapshots for an authorized actor."""
    actions = _approved_action_rows(source_system, actor=actor)
    _write_exchange_access_audit(
        "erp_exchange_export_actions",
        actor=actor,
        source_system=source_system,
        rows=actions,
        result=f"exported {len(actions)} approved action(s)",
    )
    return _serialize_csv(EXPORT_COLUMNS, actions)


def build_receipt_template_csv(
    source_system: str | None = None, *, actor: str
) -> bytes:
    """Build an unsigned fill-in receipt file for an external ERP connector."""
    actions = _approved_action_rows(source_system, actor=actor)
    key_id, _ = _receipt_verification_config()
    _write_exchange_access_audit(
        "erp_exchange_export_receipt_template",
        actor=actor,
        source_system=source_system,
        rows=actions,
        result=f"exported {len(actions)} receipt template row(s)",
    )
    receipt_rows = (
        {
            "source_system": row["source_system"],
            "external_id": row["external_id"],
            "operation_id": row["operation_id"],
            "approval_id": row["approval_id"],
            "payload_digest": row["payload_digest"],
            "receipt_attempt_id": "",
            "receipt_status": "",
            "message": "",
            "key_id": key_id,
            "signature": "",
        }
        for row in actions
    )
    return _serialize_csv(RECEIPT_COLUMNS, receipt_rows)


def _parse_receipt_rows(content: bytes) -> list[dict]:
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise ValueError("回執 CSV 不可為空")
    if len(content) > MAX_IMPORT_BYTES:
        raise ValueError(f"回執 CSV 檔案大小不可超過 {MAX_IMPORT_BYTES} bytes")
    try:
        text = bytes(content).decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise ValueError("回執 CSV 必須使用 UTF-8 編碼") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)) or set(headers) != set(RECEIPT_COLUMNS):
        raise ValueError("回執 CSV 欄位不符")
    rows = []
    seen = set()
    for line_number, raw in enumerate(reader, start=2):
        if len(rows) >= MAX_IMPORT_ROWS:
            raise ValueError(f"回執筆數不可超過 {MAX_IMPORT_ROWS}")
        if None in raw:
            raise ValueError(f"回執第 {line_number} 列欄位數量不符")
        row = {
            "source_system": normalize_source_system(raw.get("source_system")),
            "external_id": _identifier(raw.get("external_id"), "external_id"),
            "operation_id": _clean_text(
                raw.get("operation_id"), "operation_id", required=True, max_length=128
            ),
            "approval_id": _clean_text(
                raw.get("approval_id"), "approval_id", required=True, max_length=160
            ),
            "payload_digest": _clean_text(
                raw.get("payload_digest"), "payload_digest", required=True, max_length=64
            ),
            "receipt_attempt_id": _identifier(
                raw.get("receipt_attempt_id"), "receipt_attempt_id"
            ),
            "receipt_status": _clean_text(
                raw.get("receipt_status"), "receipt_status", required=True, max_length=20
            ).lower(),
            "message": _clean_text(
                raw.get("message"), "message", required=False, max_length=500
            ),
            "key_id": _identifier(raw.get("key_id"), "key_id"),
            "signature": _clean_text(
                raw.get("signature"), "signature", required=True, max_length=64
            ).lower(),
        }
        if row["receipt_status"] not in {"accepted", "rejected", "error"}:
            raise ValueError("receipt_status 僅允許 accepted/rejected/error")
        if not re.fullmatch(r"[0-9a-f]{64}", row["payload_digest"]):
            raise ValueError("payload_digest 格式不合法")
        if not re.fullmatch(r"[0-9a-f]{64}", row["signature"]):
            raise ValueError("signature 簽章格式不合法")
        if row["receipt_attempt_id"] in seen:
            raise ValueError(
                f"回執內重複 receipt_attempt_id：{row['receipt_attempt_id']}"
            )
        seen.add(row["receipt_attempt_id"])
        rows.append(row)
    if not rows:
        raise ValueError("回執 CSV 沒有資料列")
    return rows


def reconcile_receipt_csv(content: bytes, *, actor: str) -> dict:
    """Reconcile external receipts against exact local approval/effect facts."""
    summary = {"inserted": 0, "unchanged": 0, "records": []}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with database.transaction(immediate=True) as conn:
        conn.row_factory = sqlite3.Row
        actor = _require_exchange_actor(actor, conn, ERP_EXCHANGE_RECONCILE)
        expected_key_id, secret = _receipt_verification_config()
        rows = _parse_receipt_rows(content)
        for row in rows:
            if row["key_id"] != expected_key_id:
                raise ValueError("ERP 回執 key_id 不受信任")
            expected_signature = compute_receipt_signature(row, secret)
            if not hmac.compare_digest(row["signature"], expected_signature):
                raise ValueError("ERP 回執 signature 簽章驗證失敗")

            expected = conn.execute(
                """
                SELECT p.operation_id, p.approval_id, p.payload_digest,
                       p.parameters, p.resource_version, p.policy_version,
                       p.requester_username,
                       p.status, local_receipt.result
                FROM pending_approvals p
                JOIN effect_receipts local_receipt
                  ON local_receipt.operation_id = p.operation_id
                 AND local_receipt.approval_id = p.approval_id
                 AND local_receipt.payload_digest = p.payload_digest
                WHERE p.operation_id = ?
                  AND p.tool_name = 'sync_external_purchase_order'
                """,
                (row["operation_id"],),
            ).fetchone()
            if expected is None:
                raise ValueError(f"找不到 operation_id：{row['operation_id']}")
            action = _validated_action_snapshot(expected)
            if (
                action["source_system"] != row["source_system"]
                or action["external_id"] != row["external_id"]
                or expected["approval_id"] != row["approval_id"]
                or expected["payload_digest"] != row["payload_digest"]
                or expected["status"] != "approved"
            ):
                raise ValueError("回執與本機核准或執行收據不相符")

            existing_attempt = conn.execute(
                "SELECT * FROM erp_exchange_receipt_events "
                "WHERE receipt_attempt_id = ?",
                (row["receipt_attempt_id"],),
            ).fetchone()
            if existing_attempt is not None:
                comparable = {
                    key: existing_attempt[key]
                    for key in (
                        "operation_id",
                        "source_system",
                        "external_id",
                        "approval_id",
                        "payload_digest",
                        "receipt_attempt_id",
                        "receipt_status",
                        "message",
                        "key_id",
                        "signature",
                    )
                }
                submitted = {key: row[key] for key in comparable}
                if comparable != submitted:
                    raise ValueError("同一 receipt_attempt_id 的回執內容衝突")
                summary["unchanged"] += 1
            else:
                conn.execute(
                    """
                    INSERT INTO erp_exchange_receipt_events (
                        receipt_attempt_id, operation_id, source_system,
                        external_id, approval_id, payload_digest,
                        receipt_status, message, key_id, signature,
                        received_by, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["receipt_attempt_id"],
                        row["operation_id"],
                        row["source_system"],
                        row["external_id"],
                        row["approval_id"],
                        row["payload_digest"],
                        row["receipt_status"],
                        row["message"],
                        row["key_id"],
                        row["signature"],
                        actor,
                        now,
                    ),
                )
                current = conn.execute(
                    """
                    SELECT r.*,
                           CASE
                               WHEN r.receipt_attempt_id IS NOT NULL
                                AND EXISTS (
                                    SELECT 1
                                    FROM erp_exchange_receipt_events e
                                    WHERE e.receipt_attempt_id = r.receipt_attempt_id
                                      AND e.operation_id = r.operation_id
                                      AND e.source_system = r.source_system
                                      AND e.external_id = r.external_id
                                      AND e.approval_id = r.approval_id
                                      AND e.payload_digest = r.payload_digest
                                      AND e.receipt_status = r.receipt_status
                                      AND e.key_id = r.key_id
                                      AND e.signature = r.signature
                                      AND e.received_by = r.received_by
                                )
                               THEN 1 ELSE 0
                           END AS receipt_event_matched
                    FROM erp_exchange_receipts r
                    WHERE r.operation_id = ?
                    """,
                    (row["operation_id"],),
                ).fetchone()
                if current is None:
                    conn.execute(
                        """
                        INSERT INTO erp_exchange_receipts (
                            operation_id, source_system, external_id,
                            approval_id, payload_digest, receipt_attempt_id,
                            receipt_status, message, key_id, signature,
                            received_by, received_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["operation_id"],
                            row["source_system"],
                            row["external_id"],
                            row["approval_id"],
                            row["payload_digest"],
                            row["receipt_attempt_id"],
                            row["receipt_status"],
                            row["message"],
                            row["key_id"],
                            row["signature"],
                            actor,
                            now,
                        ),
                    )
                elif not (
                    current["receipt_status"] == "accepted"
                    and _receipt_is_verified(
                        dict(current),
                        event_matched=bool(current["receipt_event_matched"]),
                    )
                ):
                    conn.execute(
                        """
                        UPDATE erp_exchange_receipts
                        SET receipt_attempt_id = ?, receipt_status = ?,
                            message = ?, key_id = ?, signature = ?,
                            received_by = ?, received_at = ?
                        WHERE operation_id = ?
                        """,
                        (
                            row["receipt_attempt_id"],
                            row["receipt_status"],
                            row["message"],
                            row["key_id"],
                            row["signature"],
                            actor,
                            now,
                            row["operation_id"],
                        ),
                    )
                summary["inserted"] += 1
            summary["records"].append(dict(row))
        _write_exchange_access_audit(
            "erp_exchange_reconcile_receipts",
            actor=actor,
            source_system=None,
            rows=rows,
            result=(
                f"reconciled {summary['inserted']} new and "
                f"{summary['unchanged']} repeated receipt(s)"
            ),
            conn=conn,
        )
    return summary


def list_exchange_receipts(
    source_system: str | None = None, *, actor: str
) -> list[dict]:
    source_filter = (
        normalize_source_system(source_system)
        if source_system is not None
        else None
    )
    with sqlite3.connect(database.DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        _require_exchange_actor(actor, conn, ERP_EXCHANGE_RECONCILE)
        rows = conn.execute(
            """
            SELECT r.*,
                   CASE
                       WHEN r.receipt_attempt_id IS NOT NULL
                        AND r.key_id IS NOT NULL
                        AND r.signature IS NOT NULL
                        AND r.received_by IS NOT NULL
                        AND EXISTS (
                            SELECT 1
                            FROM erp_exchange_receipt_events verified
                            WHERE verified.receipt_attempt_id = r.receipt_attempt_id
                              AND verified.operation_id = r.operation_id
                              AND verified.source_system = r.source_system
                              AND verified.external_id = r.external_id
                              AND verified.approval_id = r.approval_id
                              AND verified.payload_digest = r.payload_digest
                              AND verified.receipt_status = r.receipt_status
                              AND verified.key_id = r.key_id
                              AND verified.signature = r.signature
                              AND verified.received_by = r.received_by
                        )
                       THEN 1 ELSE 0
                   END AS receipt_event_matched,
                   (SELECT COUNT(*) FROM erp_exchange_receipt_events e
                    WHERE e.operation_id = r.operation_id) AS attempt_count
            FROM erp_exchange_receipts r
            ORDER BY r.received_at DESC, r.operation_id
            """
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["receipt_verified"] = _receipt_is_verified(
            item, event_matched=bool(item["receipt_event_matched"])
        )
        item["trust_state"] = (
            "verified" if item["receipt_verified"] else "unverified_legacy"
        )
        result.append(item)
    if source_filter is not None:
        result = [
            row for row in result if row["source_system"] == source_filter
        ]
    return result
