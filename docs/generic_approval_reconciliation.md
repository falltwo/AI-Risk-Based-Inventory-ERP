# Generic approval reconciliation runbook

This runbook applies only to legacy generic `write`/`dangerous` tools. Protected purchase-order and ERP-exchange operations use a separate atomic transaction path.

## Trigger

Investigate when a generic approval remains `executing` after the request has finished and no matching row exists in `effect_receipts`. This means the process may have stopped after the tool effect but before the receipt and final approval state were committed.

## Safety rule

Never replay or automatically retry an `executing` approval. The current design is live at-most-once: retrying could duplicate an effect that already happened.

## Procedure

1. Stop the Web and LINE write entry points for the affected operation. Keep the original approval unchanged; do not edit its status or checksum directly.
2. Stop the application, make a filesystem copy of the SQLite database, and record the approval ID, tool name, canonical parameters, requester, approver, operation ID, and last update time.
3. Check for a local receipt with the approval ID:

   ```sql
   SELECT p.approval_id, p.tool_name, p.parameters, p.requester_username,
          p.approver, p.status, p.operation_id, p.updated_at,
          r.receipt_id, r.result, r.created_at
   FROM pending_approvals AS p
   LEFT JOIN effect_receipts AS r ON r.approval_id = p.approval_id
   WHERE p.approval_id = ?;
   ```

4. Inspect the authoritative business table and its domain history using the exact approved parameters. Action logs are supporting evidence only; absence of a log does not prove that no effect occurred.
5. Classify the incident:

   - **Effect proven applied:** do not replay. Reconcile the business record, and use a separately proposed and approved compensating action if correction is required.
   - **Effect proven absent:** do not reuse the old approval. After independent review, create a new proposal with a new operation ID.
   - **Outcome uncertain:** keep writes paused for the affected resource and restore from the verified pre-operation backup or escalate to a domain owner. Do not guess.

6. Keep the original row in `executing` as a quarantine marker. Record the evidence, operator, decision, and any new proposal/compensation ID in the incident record outside the approval table. Resume writes only after a second person verifies the reconciliation.

## Exit criteria

- The business state is verified against the approved parameters.
- No automatic replay occurred.
- Any compensation or replacement proposal has its own approval and audit trail.
- A second person reviewed the reconciliation evidence.
