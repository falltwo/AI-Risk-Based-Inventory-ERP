from __future__ import annotations

import importlib.util
import os
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "line bot" / "line_access.py"
SPEC = importlib.util.spec_from_file_location("line_access_under_test", MODULE_PATH)
line_access = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(line_access)

# ── bot_server 需要完整依賴（LINE SDK / google-genai / fastapi / backend）。
# 強制覆寫為測試值，禁止沿用執行環境中的正式憑證。
os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "test-token"
os.environ["LINE_CHANNEL_SECRET"] = "test-secret"
os.environ["GEMINI_API_KEY"] = "test-key"
_LINE_BOT_DIR = Path(__file__).resolve().parents[1] / "line bot"
if str(_LINE_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_LINE_BOT_DIR))

# conftest 已在任何 backend import 前把 ERP_DB_PATH 指到暫存目錄。
# 依賴或初始化若失敗，必須直接讓測試 collection 失敗，不可 skip 假綠。
_IMPORT_NETWORK_ATTEMPTS = []
_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex


def _deny_import_network(self, address):
    _IMPORT_NETWORK_ATTEMPTS.append(address)
    raise AssertionError(f"bot_server import attempted network access: {address!r}")


socket.socket.connect = _deny_import_network
socket.socket.connect_ex = _deny_import_network
try:
    import bot_server
finally:
    socket.socket.connect = _ORIGINAL_SOCKET_CONNECT
    socket.socket.connect_ex = _ORIGINAL_SOCKET_CONNECT_EX


def test_bot_server_import_is_pinned_to_test_credentials_and_temp_db():
    from backend import database

    assert os.environ["LINE_CHANNEL_ACCESS_TOKEN"] == "test-token"
    assert os.environ["LINE_CHANNEL_SECRET"] == "test-secret"
    assert os.environ["GEMINI_API_KEY"] == "test-key"
    assert Path(database.DB_FILE).resolve() == Path(os.environ["ERP_DB_PATH"]).resolve()
    assert Path(database.DB_FILE).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert _IMPORT_NETWORK_ATTEMPTS == []


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


class FakeGatewayResult:
    def __init__(self, status: str, data=None, message: str = ""):
        self.status = status
        self.data = data
        self.message = message

    def is_ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "data": self.data,
            "message": self.message,
            "approval_id": "",
        }


class SpyGateway:
    """記錄每次 gateway.call；決不真正執行任何工具本體。"""

    def __init__(self, result: FakeGatewayResult | None = None):
        self.calls: list[tuple] = []
        self.result = result or FakeGatewayResult("ok", data="SPY-RESULT")

    def call(self, tool_name: str, args: dict, role: str):
        self.calls.append((tool_name, args, role))
        return self.result


# ─────────────────────────────────────────────────────────────────────
# 既有測試：LINE 白名單過濾（保留）
# ─────────────────────────────────────────────────────────────────────


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


def test_line_execution_guard_rejects_write_even_if_model_names_it():
    registry = FakeRegistry()

    assert line_access.is_line_tool_allowed(
        "inventory_read", registry, "warehouse"
    )
    assert not line_access.is_line_tool_allowed(
        "inventory_write", registry, "warehouse"
    )
    assert not line_access.is_line_tool_allowed("unknown", registry, "warehouse")


def test_line_gateway_rechecks_execution_boundary():
    # 執行期防線現在位於 line_access（bot_server 僅薄薄轉發）。
    source = MODULE_PATH.read_text(encoding="utf-8")

    assert "is_line_tool_allowed(tool_name, registry, role)" in source
    assert "gateway.call(tool_name, args or {}, role=role)" in source


def test_briefing_user_ids_are_trimmed_and_deduplicated():
    assert line_access.parse_line_user_ids(" U1, U2,U1, ,U3 ") == ("U1", "U2", "U3")
    assert line_access.parse_line_user_ids(None) == ()


def test_briefing_flag_requires_explicit_truthy_value():
    assert line_access.env_flag("true") is True
    assert line_access.env_flag("ON") is True
    assert line_access.env_flag("false") is False
    assert line_access.env_flag("unexpected") is False


# ─────────────────────────────────────────────────────────────────────
# RED 批次：身分解析失敗路徑必須拒絕，不得回退 warehouse（fail-closed）
# ─────────────────────────────────────────────────────────────────────


def test_resolution_import_failure_denies_instead_of_falling_back_to_warehouse(monkeypatch):
    from backend import database

    # 明確模擬 resolver 不存在，避免未來 database 新增同名函式後測試失去意義。
    monkeypatch.delattr(database, "get_line_user_role", raising=False)
    assert bot_server._get_line_user_role("U1") is None


def test_resolution_query_exception_denies_instead_of_falling_back_to_warehouse(monkeypatch):
    from backend import database

    def boom(line_user_id: str) -> str:
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "get_line_user_role", boom, raising=False)
    # 目前實作 catch 例外後回退 "warehouse"（fail-open）；期望為 None（拒絕）。
    assert bot_server._get_line_user_role("U1") is None


def test_missing_or_blank_identity_denies_instead_of_falling_back_to_warehouse():
    # 缺失身分（None）與空白身分（""）都不應取得任何角色。
    assert bot_server._get_line_user_role("") is None
    assert bot_server._get_line_user_role(None) is None


def test_empty_resolved_role_is_treated_as_unresolved(monkeypatch):
    from backend import database

    monkeypatch.setattr(database, "get_line_user_role", lambda uid: "", raising=False)
    # 目前實作直接回傳 ""；期望正規化為 None（拒絕）。
    assert bot_server._get_line_user_role("U1") is None


def test_nonempty_user_not_found_is_treated_as_unresolved(monkeypatch):
    from backend import database

    monkeypatch.setattr(database, "get_line_user_role", lambda uid: None, raising=False)
    assert bot_server._get_line_user_role("U-not-found") is None


def test_unresolved_role_never_enters_gateway(monkeypatch):
    spy = SpyGateway()
    monkeypatch.setattr(bot_server, "gateway", spy)

    payload, executed = bot_server._build_gateway_function_response(
        "get_all_inventory", {}, role=None
    )

    # 目前實作把 None 回退為 warehouse 並呼叫 Gateway（fail-open）。
    assert spy.calls == []
    assert executed is False


def test_unresolved_role_returns_consistent_denied_payload(monkeypatch):
    spy = SpyGateway()
    monkeypatch.setattr(bot_server, "gateway", spy)

    payload, executed = bot_server._build_gateway_function_response(
        "get_all_inventory", {}, role=None
    )

    # 目前實作會執行成功（status ok）；期望一致拒絕。
    assert executed is False
    assert payload["status"] == "denied"
    assert "身分" in payload.get("error", "")
    assert spy.calls == []


@pytest.mark.parametrize(
    ("tool_name", "role"),
    [
        ("inventory_read", "unknown-role"),
        ("inventory_write", "warehouse"),
        ("payroll", "warehouse"),
        ("ledger", "warehouse"),
        ("unknown", "warehouse"),
    ],
)
def test_unknown_role_and_blocked_tools_never_enter_gateway(tool_name, role):
    spy = SpyGateway()

    payload, executed = line_access.build_line_gateway_response(
        tool_name, {}, FakeRegistry(), spy, role
    )

    assert payload["status"] == "denied"
    assert executed is False
    assert spy.calls == []


def test_actual_registry_rejects_unknown_role_before_gateway():
    from backend.tool_registry import registry

    spy = SpyGateway()
    payload, executed = line_access.build_line_gateway_response(
        "get_all_inventory", {}, registry, spy, "unknown-role"
    )

    assert payload["status"] == "denied"
    assert executed is False
    assert spy.calls == []


def test_get_ai_response_model_tool_call_with_unresolved_role_never_enters_gateway(monkeypatch):
    class FakeModels:
        def __init__(self):
            self.calls = 0

        def generate_content(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                function_call = SimpleNamespace(name="get_all_inventory", args={})
                return SimpleNamespace(
                    function_calls=[function_call],
                    text="",
                    candidates=[SimpleNamespace(content=SimpleNamespace(parts=[]))],
                )
            return SimpleNamespace(function_calls=[], text="已拒絕未解析身分的工具呼叫")

    models = FakeModels()
    spy = SpyGateway()
    monkeypatch.setattr(bot_server, "client", SimpleNamespace(models=models))
    monkeypatch.setattr(bot_server, "gateway", spy)
    monkeypatch.setattr(bot_server, "_write_line_dispatch_log", lambda *args: None)

    reply, dashboards = bot_server.get_ai_response(
        "查詢全部庫存", user_id=None, erp_role=None
    )

    assert models.calls == 2
    assert spy.calls == []
    assert dashboards == []
    assert "拒絕" in reply


def test_valid_role_and_allowed_tool_still_execute_through_gateway():
    spy = SpyGateway()

    payload, executed = line_access.build_line_gateway_response(
        "inventory_read", {"sku": "P001"}, FakeRegistry(), spy, "warehouse"
    )

    assert executed is True
    assert payload["status"] == "ok"
    assert payload["result"] == "SPY-RESULT"
    assert spy.calls == [("inventory_read", {"sku": "P001"}, "warehouse")]


def test_unresolved_identity_does_not_trigger_keyword_dashboard_bypass(monkeypatch):
    from backend import database

    dashboard_calls = []
    monkeypatch.setattr(database, "run_query", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bot_server,
        "build_low_stock_flex",
        lambda: dashboard_calls.append("low_stock"),
    )

    class FakeLineApi:
        def get_profile(self, user_id):
            return SimpleNamespace(display_name="Test")

        def reply_message_with_http_info(self, request):
            return None

    event = SimpleNamespace(
        source=SimpleNamespace(user_id="U-unresolved"), reply_token="reply-token"
    )

    # 舊路徑只看「庫存」關鍵字就直接呼叫 DB-backed dashboard builder，
    # 即使身分解析失敗、Gateway 已拒絕也會繞過授權邊界。
    bot_server._send_full_reply(
        event, FakeLineApi(), "查詢庫存", "身分解析失敗，已拒絕", []
    )

    assert dashboard_calls == []


def test_valid_warehouse_role_keeps_keyword_inventory_dashboard(monkeypatch):
    from backend import database

    dashboard_calls = []
    monkeypatch.setattr(database, "run_query", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bot_server,
        "build_low_stock_flex",
        lambda: dashboard_calls.append("low_stock"),
    )

    class FakeLineApi:
        def get_profile(self, user_id):
            return SimpleNamespace(display_name="Test")

        def reply_message_with_http_info(self, request):
            return None

    event = SimpleNamespace(
        source=SimpleNamespace(user_id="U-warehouse"), reply_token="reply-token"
    )

    bot_server._send_full_reply(
        event,
        FakeLineApi(),
        "查詢庫存",
        "ok",
        [],
        erp_role="warehouse",
    )

    assert dashboard_calls == ["low_stock"]
