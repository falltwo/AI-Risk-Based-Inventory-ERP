"""Server-side authorization policy for the tiered ERP demo.

Commercial entitlements and employee roles are separate dimensions.  This
module owns the role-to-capability contract; database-backed entitlement
resolution is added at the same boundary rather than in Streamlit widgets.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from backend import database


RISK_OVERVIEW_READ = "risk.overview.read"
RISK_ANALYSIS_READ = "risk.analysis.read"
RISK_WHAT_IF_RUN = "risk.what_if.run"
RISK_WORKSPACE_WRITE = "risk.workspace.write"
ERP_POLICY_WRITE = "erp.policy.write"
ERP_EXCHANGE_PROPOSE = "erp.exchange.propose"
PROPOSAL_EVIDENCE_READ = "proposal.evidence.read"
APPROVAL_QUEUE_READ = "approval.queue.read"
APPROVAL_DECIDE = "approval.decide"
GLOBAL_APPROVAL_DECIDE = "approval.global.decide"
ERP_EXCHANGE_EXPORT = "erp.exchange.export"
ERP_EXCHANGE_RECONCILE = "erp.exchange.reconcile"

L1_MONITOR = "l1_monitor"
L2_DECISION = "l2_decision"
L3_GOVERNED_ACTION = "l3_governed_action"

_CAPABILITY_ENTITLEMENT = {
    RISK_OVERVIEW_READ: L1_MONITOR,
    RISK_ANALYSIS_READ: L2_DECISION,
    RISK_WHAT_IF_RUN: L2_DECISION,
    RISK_WORKSPACE_WRITE: L2_DECISION,
    ERP_POLICY_WRITE: L3_GOVERNED_ACTION,
    ERP_EXCHANGE_PROPOSE: L2_DECISION,
    PROPOSAL_EVIDENCE_READ: L3_GOVERNED_ACTION,
    APPROVAL_QUEUE_READ: L3_GOVERNED_ACTION,
    APPROVAL_DECIDE: L3_GOVERNED_ACTION,
    GLOBAL_APPROVAL_DECIDE: L3_GOVERNED_ACTION,
    ERP_EXCHANGE_EXPORT: L3_GOVERNED_ACTION,
    ERP_EXCHANGE_RECONCILE: L3_GOVERNED_ACTION,
}

_ALL_CAPABILITIES = frozenset(_CAPABILITY_ENTITLEMENT)


_ROLE_CAPABILITIES = {
    "risk_viewer": frozenset({RISK_OVERVIEW_READ}),
    "supply_planner": frozenset(
        {
            RISK_OVERVIEW_READ,
            RISK_ANALYSIS_READ,
            RISK_WHAT_IF_RUN,
            RISK_WORKSPACE_WRITE,
            ERP_EXCHANGE_PROPOSE,
        }
    ),
    "procurement_approver": frozenset(
        {
            RISK_OVERVIEW_READ,
            PROPOSAL_EVIDENCE_READ,
            APPROVAL_QUEUE_READ,
            APPROVAL_DECIDE,
            ERP_EXCHANGE_EXPORT,
            ERP_EXCHANGE_RECONCILE,
        }
    ),
    # Preserve the existing demo accounts while routing the new accounts
    # through the narrower role bundles above.
    "warehouse": frozenset(
        {
            RISK_OVERVIEW_READ,
            RISK_ANALYSIS_READ,
            RISK_WHAT_IF_RUN,
            RISK_WORKSPACE_WRITE,
            ERP_POLICY_WRITE,
            ERP_EXCHANGE_PROPOSE,
            APPROVAL_QUEUE_READ,
            ERP_EXCHANGE_EXPORT,
            ERP_EXCHANGE_RECONCILE,
        }
    ),
    "admin": _ALL_CAPABILITIES,
}


def capabilities_for_role(role: str) -> set[str]:
    """Return an isolated capability set; unknown roles are denied by default."""
    return set(_ROLE_CAPABILITIES.get(str(role or "").strip(), frozenset()))


@dataclass(frozen=True)
class AccessContext:
    username: str
    role: str
    name: str
    organization_id: str
    entitlements: frozenset[str]
    capabilities: frozenset[str]

    def can(self, capability: str) -> bool:
        return capability in self.capabilities


def load_principal(
    username: str, *, conn: sqlite3.Connection | None = None
) -> AccessContext | None:
    """Reload one principal from SQLite; missing identity or membership denies."""
    username = str(username or "").strip()
    if not username:
        return None

    def _load(active_conn: sqlite3.Connection) -> AccessContext | None:
        row = active_conn.execute(
            """
            SELECT u.username, u.role, u.name, membership.organization_id
            FROM users u
            JOIN user_organizations membership
              ON membership.username = u.username
            WHERE u.username = ?
            """,
            (username,),
        ).fetchone()
        if row is None:
            return None
        organization_id = row[3]
        deployment = active_conn.execute(
            "SELECT value FROM app_metadata "
            "WHERE key = 'deployment_organization_id'"
        ).fetchone()
        if deployment is None or deployment[0] != organization_id:
            return None
        entitlement_rows = active_conn.execute(
            """
            SELECT entitlement_key
            FROM organization_entitlements
            WHERE organization_id = ? AND enabled = 1
            """,
            (organization_id,),
        ).fetchall()
        entitlements = frozenset(item[0] for item in entitlement_rows)
        effective = frozenset(
            capability
            for capability in capabilities_for_role(row[1])
            if _CAPABILITY_ENTITLEMENT.get(capability) in entitlements
        )
        return AccessContext(
            username=row[0],
            role=row[1],
            name=row[2],
            organization_id=organization_id,
            entitlements=entitlements,
            capabilities=effective,
        )

    if conn is not None:
        return _load(conn)
    with sqlite3.connect(database.DB_FILE) as owned_conn:
        return _load(owned_conn)


def has_capability(
    username: str, capability: str, *, conn: sqlite3.Connection | None = None
) -> bool:
    principal = load_principal(username, conn=conn)
    return bool(principal and principal.can(capability))


def require_capability(
    username: str, capability: str, *, conn: sqlite3.Connection | None = None
) -> AccessContext:
    principal = load_principal(username, conn=conn)
    if principal is None or not principal.can(capability):
        raise PermissionError(f"使用者沒有必要權限：{capability}")
    return principal


def require_any_capability(
    username: str,
    capabilities: set[str] | frozenset[str] | tuple[str, ...],
    *,
    conn: sqlite3.Connection | None = None,
) -> AccessContext:
    """Require at least one capability while still resolving identity live."""
    requested = frozenset(capabilities)
    if not requested:
        raise ValueError("至少需要指定一項 capability")
    principal = load_principal(username, conn=conn)
    if principal is None or requested.isdisjoint(principal.capabilities):
        raise PermissionError(
            "使用者沒有任何必要權限：" + ", ".join(sorted(requested))
        )
    return principal
