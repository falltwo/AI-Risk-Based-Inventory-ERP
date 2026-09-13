"""LINE 身分必須映射到仍有效的 ERP Principal。"""

import pytest

from backend import database
from backend.access_control import Principal, resolve_line_principal


@pytest.fixture
def identity_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "line-identity.db"))
    monkeypatch.setenv("ERP_DEMO_MODE", "1")
    database.init_db()


def test_unbound_line_identity_is_denied(identity_db):
    assert resolve_line_principal("U-unbound") is None


def test_line_identity_resolves_only_enabled_live_principal(identity_db):
    database.run_query(
        "INSERT INTO line_user_identities (line_user_id, username, enabled) VALUES (?, ?, 1)",
        ("U-planner", "planner"),
        fetch=False,
    )

    principal = resolve_line_principal("U-planner")
    assert isinstance(principal, Principal)
    assert principal.username == "planner"

    database.run_query(
        "UPDATE line_user_identities SET enabled=0 WHERE line_user_id='U-planner'",
        fetch=False,
    )
    assert resolve_line_principal("U-planner") is None


def test_line_identity_denies_revoked_erp_membership(identity_db):
    database.run_query(
        "INSERT INTO line_user_identities (line_user_id, username, enabled) VALUES (?, ?, 1)",
        ("U-viewer", "viewer"),
        fetch=False,
    )
    database.run_query(
        "DELETE FROM user_organizations WHERE username='viewer'", fetch=False
    )
    assert resolve_line_principal("U-viewer") is None
