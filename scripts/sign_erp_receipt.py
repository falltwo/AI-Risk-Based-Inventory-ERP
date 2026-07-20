"""Sign an ERP receipt template as the external connector boundary.

This helper is for a demo connector or a separately controlled ERP-side host.
The Streamlit application verifies the signature but never exposes this helper
or the shared secret through its UI.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
from pathlib import Path
import uuid

from backend.erp_exchange import RECEIPT_COLUMNS, compute_receipt_signature


def sign_receipt_template(
    content: bytes,
    *,
    status: str,
    message: str,
    key_id: str,
    secret: str,
) -> bytes:
    """Fill and sign every action in an unsigned receipt template."""
    if status not in {"accepted", "rejected", "error"}:
        raise ValueError("status must be accepted, rejected, or error")
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("ERP_EXCHANGE_RECEIPT_HMAC_SECRET must be at least 32 bytes")
    text = content.decode("utf-8-sig", errors="strict")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != list(RECEIPT_COLUMNS):
        raise ValueError("input is not an ERP receipt template")

    rows = []
    for raw in reader:
        row = {column: raw.get(column, "") for column in RECEIPT_COLUMNS}
        row["receipt_attempt_id"] = f"erp-attempt-{uuid.uuid4().hex}"
        row["receipt_status"] = status
        row["message"] = message
        row["key_id"] = key_id
        row["signature"] = compute_receipt_signature(row, secret)
        rows.append(row)
    if not rows:
        raise ValueError("receipt template has no approved actions")

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=list(RECEIPT_COLUMNS), lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fill and HMAC-sign an ERP receipt template."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--status", choices=("accepted", "rejected", "error"), required=True
    )
    parser.add_argument("--message", default="")
    args = parser.parse_args()

    key_id = os.getenv("ERP_EXCHANGE_RECEIPT_KEY_ID", "").strip()
    secret = os.getenv("ERP_EXCHANGE_RECEIPT_HMAC_SECRET", "")
    if not key_id:
        parser.error("ERP_EXCHANGE_RECEIPT_KEY_ID is required")
    try:
        signed = sign_receipt_template(
            args.input.read_bytes(),
            status=args.status,
            message=args.message,
            key_id=key_id,
            secret=secret,
        )
        args.output.write_bytes(signed)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
