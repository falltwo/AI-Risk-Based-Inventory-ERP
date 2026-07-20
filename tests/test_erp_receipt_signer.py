"""Contract for the external ERP receipt-signing helper."""

import csv
import io

from backend.erp_exchange import (
    RECEIPT_COLUMNS,
    compute_receipt_signature,
)
from scripts.sign_erp_receipt import sign_receipt_template


def test_signer_adds_unique_attempt_and_valid_hmac():
    template_row = {
        "source_system": "odoo-demo",
        "external_id": "odoo.po_1",
        "operation_id": "erp-csv-sync:abc",
        "approval_id": "PENDING-1",
        "payload_digest": "a" * 64,
        "receipt_attempt_id": "",
        "receipt_status": "",
        "message": "",
        "key_id": "test-key-v1",
        "signature": "",
    }
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=list(RECEIPT_COLUMNS), lineterminator="\n"
    )
    writer.writeheader()
    writer.writerow(template_row)
    secret = "test-only-receipt-secret-with-at-least-32-bytes"

    signed = sign_receipt_template(
        ("\ufeff" + output.getvalue()).encode("utf-8"),
        status="accepted",
        message="ERP import OK",
        key_id="test-key-v1",
        secret=secret,
    )

    row = list(csv.DictReader(io.StringIO(signed.decode("utf-8-sig"))))[0]
    assert row["receipt_attempt_id"].startswith("erp-attempt-")
    assert row["receipt_status"] == "accepted"
    assert row["signature"] == compute_receipt_signature(row, secret)
