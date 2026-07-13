from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "line bot" / "line_access.py"
SPEC = importlib.util.spec_from_file_location("line_access_under_test", MODULE_PATH)
line_access = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(line_access)


def _tool(name):
    def tool():
        return None

    tool.__name__ = name
    return tool


class FakeRegistry:
    def __init__(self):
        self.info = {
            "inventory_read": {"module": "inventory", "risk_level": "read_only"},
            "inventory_suggest": {"module": "inventory", "risk_level": "suggestion"},
            "inventory_write": {"module": "inventory", "risk_level": "write"},
            "payroll": {"module": "hr", "risk_level": "read_only"},
            "ledger": {"module": "finance", "risk_level": "read_only"},
            "sales_only": {"module": "orders", "risk_level": "read_only"},
        }

    def get_tool_info(self, name):
        return self.info.get(name)

    def is_allowed(self, name, role):
        return role == "warehouse" and name != "sales_only"


def test_line_tool_filter_is_fail_closed():
    names = [
        "inventory_read",
        "inventory_suggest",
        "inventory_write",
        "payroll",
        "ledger",
        "sales_only",
        "unknown",
    ]
    selected = line_access.build_line_tools(
        [_tool(name) for name in names], FakeRegistry(), role="warehouse"
    )
    assert [tool.__name__ for tool in selected] == [
        "inventory_read",
        "inventory_suggest",
    ]


def test_briefing_user_ids_are_trimmed_and_deduplicated():
    assert line_access.parse_line_user_ids(" U1, U2,U1, ,U3 ") == ("U1", "U2", "U3")
    assert line_access.parse_line_user_ids(None) == ()


def test_briefing_flag_requires_explicit_truthy_value():
    assert line_access.env_flag("true") is True
    assert line_access.env_flag("ON") is True
    assert line_access.env_flag("false") is False
    assert line_access.env_flag("unexpected") is False
