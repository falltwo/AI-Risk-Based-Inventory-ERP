"""
backend/tool_gateway.py
Tool Gateway — 所有工具呼叫的統一入口

核心流程：
    收到呼叫 → 檢查工具是否存在 → 檢查角色權限 → 寫 log → 執行或攔截

使用方式：
    from backend.tool_gateway import gateway
    result = gateway.call("check_inventory", {"product_id": "P001"}, role="sales")
"""

import traceback
from datetime import datetime
from backend.tool_registry import registry
from backend.agent_registry import get_tools_for_agent, get_agent
from backend import tools_mapping


# ── PII 遮罩（F5）：hr 模組工具參數不在 log 明文落地 ───────────
_HR_SENSITIVE_KEYS = {"employee_id", "emp_id", "employeeId", "period", "base_salary", "bonus", "deduction"}


def _mask_args_for_log(tool_name: str, args: dict) -> dict:
    """對 hr 模組工具的敏感欄位做遮罩，避免 PII 明文落地稽核紀錄。"""
    if not args:
        return args
    info = registry.get_tool_info(tool_name)
    if not info or info.get("module") != "hr":
        return args
    masked = {}
    for k, v in args.items():
        if k in _HR_SENSITIVE_KEYS:
            masked[k] = "E***" if "id" in k.lower() else "****"
        else:
            masked[k] = v
    return masked


# ── Log 介面（C 實作 DB 版本後替換） ──────────────────────────

def _write_log(tool_name: str, args: dict, role: str, result: str, success: bool):
    """
    寫入工具呼叫紀錄。hr 模組工具的敏感參數會先遮罩（F5）。
    """
    import sys
    masked_args = _mask_args_for_log(tool_name, args)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "OK" if success else "FAIL"
    line = f"[GATEWAY LOG] {timestamp} | {status} | role={role} | tool={tool_name} | args={masked_args} | result={str(result)[:80]}\n"
    sys.stdout.buffer.write(line.encode("utf-8", errors="replace"))

    try:
        from backend.agent_logger import write_action_log
        write_action_log(tool_name, masked_args, role, result, success)
    except Exception as e:
        sys.stderr.write(f"[GATEWAY ERROR] Failed to write action log to DB: {e}\n")


def _create_pending_approval(tool_name: str, args: dict, role: str) -> str:
    """
    將 write/dangerous 工具呼叫建立為待審批項目。
    回傳 approval_id。
    """
    import sys
    try:
        from backend.agent_logger import create_pending_approval
        approval_id = create_pending_approval(tool_name, args, role)
    except Exception as e:
        approval_id = f"PENDING-{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{tool_name}"
        sys.stderr.write(f"[GATEWAY ERROR] Failed to create pending approval in DB: {e}\n")

    line = f"[GATEWAY PENDING] {approval_id} | role={role} | tool={tool_name} | args={args}\n"
    sys.stdout.buffer.write(line.encode("utf-8", errors="replace"))

    return approval_id


# ── Gateway 回應格式 ───────────────────────────────────────────

class GatewayResult:
    """Gateway 統一回應格式"""
    def __init__(self, status: str, data=None, message: str = "", approval_id: str = ""):
        self.status      = status       # "ok" | "pending" | "denied" | "error"
        self.data        = data         # 工具執行結果
        self.message     = message      # 說明訊息
        self.approval_id = approval_id  # write 工具被攔截時的審批 ID

    def is_ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return {
            "status":      self.status,
            "data":        self.data,
            "message":     self.message,
            "approval_id": self.approval_id,
        }

    def __repr__(self):
        return f"GatewayResult(status={self.status}, message={self.message!r})"


# ── Tool Gateway ───────────────────────────────────────────────

class ToolGateway:
    """
    所有工具呼叫的統一入口。
    B（總管 Agent）、E（LINE Bot）都要透過這裡呼叫工具，不能直接呼叫 tools_mapping。
    """

    def call(self, tool_name: str, args: dict, role: str, agent_name: str = "") -> GatewayResult:
        """
        呼叫工具的主入口。

        Args:
            tool_name  : 工具名稱（對應 tools_mapping 的 key）
            args       : 工具參數（dict）
            role       : 呼叫者角色（admin / warehouse / sales / hr）
            agent_name : 呼叫者 Agent ID（選填）。填入時額外檢查 Agent 白名單。
                         例如：inventory_agent、sales_agent、orchestrator

        Returns:
            GatewayResult
        """

        # Step 1：確認工具存在
        if not registry.tool_exists(tool_name):
            msg = f"工具「{tool_name}」不在工具登記表中，請確認名稱是否正確。"
            _write_log(tool_name, args, role, msg, success=False)
            return GatewayResult(status="error", message=msg)

        if tool_name not in tools_mapping:
            msg = f"工具「{tool_name}」已登記但尚未實作，請聯絡開發人員。"
            _write_log(tool_name, args, role, msg, success=False)
            return GatewayResult(status="error", message=msg)

        # Step 2：確認 Agent 白名單（若有指定 agent_name）
        if agent_name:
            agent_meta = get_agent(agent_name)
            if agent_meta is None:
                msg = f"Agent「{agent_name}」不在登記表中，請確認 agent_id 是否正確。"
                _write_log(tool_name, args, role, msg, success=False)
                return GatewayResult(status="error", message=msg)

            allowed_tools = get_tools_for_agent(agent_name)
            if tool_name not in allowed_tools:
                msg = (
                    f"Agent「{agent_meta['name_zh']}（{agent_name}）」"
                    f"無權使用工具「{tool_name}」，此工具不在該 Agent 的白名單內。"
                )
                _write_log(tool_name, args, role, msg, success=False)
                return GatewayResult(status="denied", message=msg)

        # Step 4：確認角色有權限
        if not registry.is_allowed(tool_name, role):
            tool_info = registry.get_tool_info(tool_name)
            allowed   = "、".join(tool_info["allowed_roles"])
            msg = f"角色「{role}」無權使用工具「{tool_name}」，此工具僅允許：{allowed}。"
            _write_log(tool_name, args, role, msg, success=False)
            return GatewayResult(status="denied", message=msg)

        # Step 5：依風險等級決定行為
        risk_level = registry.get_risk_level(tool_name)

        if risk_level in ("read_only", "suggestion"):
            # 直接執行
            return self._execute(tool_name, args, role)

        elif risk_level == "write":
            # 攔截，送審批
            approval_id = _create_pending_approval(tool_name, args, role)
            msg = (
                f"工具「{tool_name}」為寫入操作，已建立審批單（{approval_id}），"
                f"請管理者至 Dashboard 核准後才會執行。"
            )
            _write_log(tool_name, args, role, msg, success=True)
            return GatewayResult(status="pending", message=msg, approval_id=approval_id)

        elif risk_level == "dangerous":
            # 攔截，送審批（並加警告）
            approval_id = _create_pending_approval(tool_name, args, role)
            msg = (
                f"⚠️ 工具「{tool_name}」為高風險操作，已建立審批單（{approval_id}），"
                f"需管理者核准後才會執行。"
            )
            _write_log(tool_name, args, role, msg, success=True)
            return GatewayResult(status="pending", message=msg, approval_id=approval_id)

        else:
            msg = f"工具「{tool_name}」的風險等級「{risk_level}」未定義，請更新 tool_classification.py。"
            _write_log(tool_name, args, role, msg, success=False)
            return GatewayResult(status="error", message=msg)

    def _execute(self, tool_name: str, args: dict, role: str) -> GatewayResult:
        """實際執行工具函式"""
        try:
            import inspect
            func   = tools_mapping[tool_name]
            sig = inspect.signature(func)
            has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if has_kwargs:
                filtered_args = args or {}
            else:
                filtered_args = {k: v for k, v in (args or {}).items() if k in sig.parameters}
            result = func(**filtered_args)
            _write_log(tool_name, args, role, result, success=True)
            return GatewayResult(status="ok", data=result)
        except Exception as e:
            msg = f"工具「{tool_name}」執行失敗：{e}\n{traceback.format_exc()}"
            _write_log(tool_name, args, role, msg, success=False)
            return GatewayResult(status="error", message=msg)

    def approve_action(self, approval_id: str, approver: str = "admin") -> GatewayResult:
        """
        核准審批項目，並真正執行該工具操作。
        """
        from backend.agent_logger import get_pending_approval_by_id, update_approval_status
        
        item = get_pending_approval_by_id(approval_id)
        if not item:
            return GatewayResult(status="error", message=f"找不到 ID 為 {approval_id} 的審批項目。")
            
        if item["status"] != "pending":
            return GatewayResult(status="error", message=f"該審批項目的狀態為 {item['status']}，無法重複核准。")
            
        tool_name = item["tool_name"]
        args = item["parameters"]
        role = item["requester"]
        
        # 真正執行操作
        result_gateway = self._execute(tool_name, args, role)
        if result_gateway.status != "ok":
            return result_gateway
        
        # Only mark approved after the underlying write succeeds.
        update_approval_status(approval_id, approver, "approved")
        return result_gateway

    def reject_action(self, approval_id: str, reason: str, approver: str = "admin") -> GatewayResult:
        """
        拒絕審批項目，操作作廢並記錄原因。
        """
        from backend.agent_logger import get_pending_approval_by_id, update_approval_status, write_action_log
        
        item = get_pending_approval_by_id(approval_id)
        if not item:
            return GatewayResult(status="error", message=f"找不到 ID 為 {approval_id} 的審批項目。")
            
        if item["status"] != "pending":
            return GatewayResult(status="error", message=f"該審批項目的狀態為 {item['status']}，無法重複拒絕。")
            
        tool_name = item["tool_name"]
        args = item["parameters"]
        role = item["requester"]
        
        # 將狀態更新為 rejected 並存入拒絕原因
        update_approval_status(approval_id, approver, "rejected", reason=reason)
        
        # 記錄作廢日誌，包含原因
        msg = f"操作遭管理者「{approver}」拒絕，原因：{reason}。該工具執行已作廢。"
        write_action_log(tool_name, args, role, msg, success=False)
        
        return GatewayResult(status="denied", message=msg)

    def execute_approved(self, tool_name: str, args: dict, role: str = "admin") -> GatewayResult:
        """
        僅供已人工核准的補償操作直接執行（跳過 write 攔截），並寫入 action log。
        適用場景：Dashboard 沖銷/重試，管理員已人工確認，不需再送審批。
        """
        return self._execute(tool_name, args, role)


# 全域單例，直接 import 使用
gateway = ToolGateway()
