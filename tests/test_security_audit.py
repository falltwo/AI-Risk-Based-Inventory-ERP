from datetime import date

import pytest

from backend.access_control import SECURITY_AUDIT_READ, capabilities_for_role
from backend.database import init_db, run_query
from backend.security_audit import list_auth_events


def _seed_admin() -> None:
    init_db()
    # 其他安全測試可能會撤銷 demo 組織資料；此測試明確建立所需授權邊界。
    run_query(
        "INSERT OR REPLACE INTO users (username, password, role, name) VALUES (?, ?, ?, ?)",
        ("admin", "not-used-in-this-test", "admin", "系統管理員"),
        fetch=False,
    )
    run_query(
        "INSERT OR REPLACE INTO user_organizations (username, organization_id) VALUES (?, ?)",
        ("admin", "demo-org"),
        fetch=False,
    )
    run_query(
        "INSERT OR REPLACE INTO app_metadata (key, value) VALUES (?, ?)",
        ("deployment_organization_id", "demo-org"),
        fetch=False,
    )
    run_query(
        "INSERT OR REPLACE INTO organization_entitlements "
        "(organization_id, entitlement_key, enabled) VALUES (?, ?, 1)",
        ("demo-org", "l3_governed_action"),
        fetch=False,
    )


def test_only_admin_receives_security_audit_capability():
    assert SECURITY_AUDIT_READ in capabilities_for_role("admin")
    assert SECURITY_AUDIT_READ not in capabilities_for_role("warehouse")
    assert SECURITY_AUDIT_READ not in capabilities_for_role("procurement_approver")


def test_admin_can_filter_audited_events_without_password_data():
    _seed_admin()
    run_query(
        "INSERT INTO auth_events (username, event_type, occurred_at) VALUES (?, ?, ?)",
        ("audit-target", "login_locked", "2026-09-13 10:00:00"),
        fetch=False,
    )
    run_query(
        "INSERT INTO auth_events (username, event_type, occurred_at) VALUES (?, ?, ?)",
        ("someone-else", "login_failed", "2026-09-13 10:01:00"),
        fetch=False,
    )

    result = list_auth_events(
        "admin",
        target_username="audit-target",
        event_type="login_locked",
        start_date=date(2026, 9, 13),
        end_date=date(2026, 9, 13),
    )

    assert result == [
        {"帳號": "audit-target", "事件": "帳號已鎖定", "時間": "2026-09-13 10:00:00"}
    ]
    assert "password" not in str(result).lower()


def test_non_admin_is_denied_security_audit_access():
    _seed_admin()
    with pytest.raises(PermissionError):
        list_auth_events("wh1")
