"""Regression tests for the backend authorization execution boundary."""

from backend.auth import authorized_role, check_permission


def test_permission_without_gateway_context_is_denied():
    assert check_permission(["warehouse"]) is False
    assert check_permission(["admin"]) is False


def test_verified_role_is_scoped_to_context():
    with authorized_role("warehouse"):
        assert check_permission(["warehouse"]) is True
        assert check_permission(["sales"]) is False

    assert check_permission(["warehouse"]) is False


def test_admin_override_requires_verified_context():
    with authorized_role("admin"):
        assert check_permission(["warehouse"]) is True


def test_nested_context_restores_outer_role():
    with authorized_role("warehouse"):
        with authorized_role("sales"):
            assert check_permission(["sales"]) is True
            assert check_permission(["warehouse"]) is False
        assert check_permission(["warehouse"]) is True
