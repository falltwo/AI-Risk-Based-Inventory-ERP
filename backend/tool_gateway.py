"""
backend/tool_gateway.py
Tool Gateway — 所有工具呼叫的統一入口

核心流程：
    收到呼叫 → 檢查工具是否存在 → 檢查角色權限 → 寫 log → 執行或攔截

使用方式：
    from backend.tool_gateway import gateway
    result = gateway.call("check_inventory", {"product_id": "P001"}, role="sales")
"""

import hashlib
import hmac
import json
import sqlite3
import traceback
from datetime import datetime
from backend.tool_registry import registry
from backend.agent_registry import get_tools_for_agent, get_agent
from backend import tools_mapping
from backend.erp_exchange import ERP_EXCHANGE_POLICY_VERSION


PO_APPROVAL_POLICY_VERSION = "po-approval-v2"


def canonical_payload_digest(
    *,
    tool_name: str,
    args: dict,
    resource_version: str,
    policy_version: str,
    requester_username: str | None = None,
) -> str:
    """Return a stable digest of the exact action that a person will approve."""
    target = (args or {}).get("po_id")
    if tool_name == "sync_external_purchase_order":
        target = {
            "source_system": (args or {}).get("source_system"),
            "external_id": (args or {}).get("external_id"),
        }
    payload = {
        "action": tool_name,
        "target": target,
        "parameters": args or {},
        "resource_version": resource_version,
        "policy_version": policy_version,
        "requester_username": str(requester_username or "").strip() or None,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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

def _write_log(
    tool_name: str,
    args: dict,
    role: str,
    result: str,
    success: bool,
    *,
    conn=None,
):
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
        write_action_log(
            tool_name, masked_args, role, result, success, conn=conn
        )
    except Exception as e:
        sys.stderr.write(f"[GATEWAY ERROR] Failed to write action log to DB: {e}\n")


def _create_pending_approval(
    tool_name: str,
    args: dict,
    role: str,
    *,
    requester_username: str | None = None,
    operation_id: str | None = None,
    resource_version: str = "unspecified",
    policy_version: str = PO_APPROVAL_POLICY_VERSION,
) -> str:
    """
    將 write/dangerous 工具呼叫建立為待審批項目。
    回傳 approval_id。
    """
    import sys
    from backend.agent_logger import create_pending_approval

    approval_id = create_pending_approval(
        tool_name,
        args,
        role,
        requester_username=requester_username,
        operation_id=operation_id,
        resource_version=resource_version,
        policy_version=policy_version,
    )

    line = f"[GATEWAY PENDING] {approval_id} | role={role} | tool={tool_name} | args={args}\n"
    sys.stdout.buffer.write(line.encode("utf-8", errors="replace"))

    return approval_id


def _replay_protected_operation(
    operation_id: str,
    args: dict,
    *,
    expected_tool: str,
    expected_resource_version: str,
    expected_policy_version: str,
    requester_username: str | None = None,
) -> "GatewayResult | None":
    """Return the durable state for an existing protected operation."""
    from backend.database import run_query

    rows = run_query(
        """
        SELECT approval_id, tool_name, status, payload_digest,
               resource_version, policy_version, requester_username
        FROM pending_approvals
        WHERE operation_id = ?
        """,
        (operation_id,),
    )
    if not rows:
        return None

    (
        approval_id,
        tool_name,
        status,
        stored_digest,
        resource_version,
        policy_version,
        stored_requester_username,
    ) = rows[0]
    if (
        tool_name != expected_tool
        or resource_version != expected_resource_version
        or policy_version != expected_policy_version
        or not stored_digest
    ):
        return GatewayResult(
            status="error",
            message="既有 operation_id 的審批綁定不完整或類型不符。",
            approval_id=approval_id,
        )

    submitted_requester = str(requester_username or "").strip()
    stored_requester = str(stored_requester_username or "").strip()
    if (
        not submitted_requester
        or not stored_requester
        or not hmac.compare_digest(submitted_requester, stored_requester)
    ):
        return GatewayResult(
            status="denied",
            message="operation_id 已綁定其他提案人，拒絕重放。",
            approval_id=approval_id,
        )

    submitted_digest = canonical_payload_digest(
        tool_name=tool_name,
        args=args,
        resource_version=resource_version,
        policy_version=policy_version,
        requester_username=stored_requester,
    )
    if not hmac.compare_digest(stored_digest, submitted_digest):
        return GatewayResult(
            status="error",
            message="operation_id 已綁定不同的受保護操作內容。",
            approval_id=approval_id,
        )

    if status == "pending":
        return GatewayResult(
            status="pending",
            message=f"此操作已在等待審批（{approval_id}），尚未執行。",
            approval_id=approval_id,
        )
    if status == "rejected":
        return GatewayResult(
            status="denied",
            message=f"此操作已被拒絕（{approval_id}），不會執行。",
            approval_id=approval_id,
        )
    if status == "approved":
        receipts = run_query(
            """
            SELECT result FROM effect_receipts
            WHERE operation_id = ? AND approval_id = ? AND payload_digest = ?
            """,
            (operation_id, approval_id, stored_digest),
        )
        if not receipts:
            return GatewayResult(
                status="error",
                message="審批已核准但找不到相符的執行收據。",
                approval_id=approval_id,
            )
        try:
            result = json.loads(receipts[0][0])
        except (TypeError, json.JSONDecodeError):
            return GatewayResult(
                status="error",
                message="既有執行收據格式損壞。",
                approval_id=approval_id,
            )
        return GatewayResult(
            status="ok", data=result, approval_id=approval_id
        )

    return GatewayResult(
        status="error",
        message=f"此操作目前狀態為 {status}，請稍後再查。",
        approval_id=approval_id,
    )


def _replay_purchase_order_operation(
    operation_id: str, args: dict, *, requester_username: str | None = None
) -> "GatewayResult | None":
    """Backward-compatible wrapper for protected PO creation replay."""
    return _replay_protected_operation(
        operation_id,
        args,
        expected_tool="create_purchase_order",
        expected_resource_version="absent",
        expected_policy_version=PO_APPROVAL_POLICY_VERSION,
        requester_username=requester_username,
    )


def _replay_protected_operation_from_storage(
    operation_id: str,
    args: dict,
    *,
    expected_tool: str,
    expected_policy_version: str,
    requester_username: str | None = None,
) -> "GatewayResult | None":
    """Replay before consulting mutable current resource state."""
    from backend.database import run_query

    rows = run_query(
        """
        SELECT tool_name, resource_version, policy_version
        FROM pending_approvals WHERE operation_id = ?
        """,
        (operation_id,),
    )
    if not rows:
        return None
    tool_name, resource_version, policy_version = rows[0]
    if (
        tool_name != expected_tool
        or policy_version != expected_policy_version
        or not resource_version
    ):
        return GatewayResult(
            status="error",
            message="既有 operation_id 的審批類型或政策版本不符。",
        )
    return _replay_protected_operation(
        operation_id,
        args,
        expected_tool=expected_tool,
        expected_resource_version=resource_version,
        expected_policy_version=expected_policy_version,
        requester_username=requester_username,
    )


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

    def call(
        self,
        tool_name: str,
        args: dict,
        role: str,
        agent_name: str = "",
        *,
        actor: str | None = None,
        operation_id: str | None = None,
    ) -> GatewayResult:
        """
        呼叫工具的主入口。

        Args:
            tool_name  : 工具名稱（對應 tools_mapping 的 key）
            args       : 工具參數（dict）
            role       : 呼叫者角色（admin / warehouse / sales / hr）
            actor      : 伺服器端登入帳號；所有 write/dangerous 操作必填
            agent_name : 呼叫者 Agent ID（選填）。填入時額外檢查 Agent 白名單。
                         例如：inventory_agent、sales_agent、orchestrator

        Returns:
            GatewayResult
        """

        if not isinstance(args, dict):
            return GatewayResult(status="error", message="工具參數必須是物件格式。")
        private_keys = [key for key in args if str(key).startswith("_")]
        if private_keys:
            return GatewayResult(
                status="error",
                message="工具參數不得包含保留的內部欄位。",
            )
        args = dict(args)
        protected_po = tool_name in {
            "create_purchase_order",
            "sync_external_purchase_order",
        }

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

        if protected_po:
            from backend.access_control import (
                ERP_EXCHANGE_PROPOSE,
                load_principal,
            )

            principal = load_principal(actor or "")
            if (
                principal is None
                or principal.role != role
                or not principal.can(ERP_EXCHANGE_PROPOSE)
            ):
                msg = "受保護採購操作需要與登入身分一致的提案權限。"
                _write_log(tool_name, args, role, msg, success=False)
                return GatewayResult(status="denied", message=msg)
            actor = principal.username

        # Step 5：依風險等級決定行為
        risk_level = registry.get_risk_level(tool_name)

        if risk_level in {"write", "dangerous"} and not protected_po:
            from backend.access_control import load_principal

            principal = load_principal(actor or "")
            if principal is None or principal.role != role:
                msg = "寫入操作需要與登入身分一致的可驗證提案人。"
                _write_log(tool_name, args, role, msg, success=False)
                return GatewayResult(status="denied", message=msg)
            actor = principal.username

        if risk_level in ("read_only", "suggestion"):
            # 直接執行
            return self._execute(tool_name, args, role)

        elif risk_level == "write":
            # 攔截，送審批
            try:
                resource_version = "unspecified"
                policy_version = PO_APPROVAL_POLICY_VERSION
                if protected_po:
                    operation_id = str(operation_id or "").strip()
                    if not operation_id:
                        raise ValueError(
                            "受保護採購操作必須提供穩定的 operation_id。"
                        )
                    if tool_name == "create_purchase_order":
                        po_id = str(args.get("po_id") or "").strip()
                        if not po_id:
                            raise ValueError("採購單號不可為空白。")
                        resource_version = "absent"
                        policy_version = PO_APPROVAL_POLICY_VERSION
                    else:
                        from backend.erp_exchange import (
                            build_exchange_operation_id,
                            exchange_resource_version,
                            get_exchange_record,
                        )

                        replay = _replay_protected_operation_from_storage(
                            operation_id,
                            args,
                            expected_tool=tool_name,
                            expected_policy_version=ERP_EXCHANGE_POLICY_VERSION,
                            requester_username=actor,
                        )
                        if replay is not None:
                            return replay
                        source_system = str(args.get("source_system") or "").strip()
                        external_id = str(args.get("external_id") or "").strip()
                        record = get_exchange_record(source_system, external_id)
                        if record is None:
                            raise ValueError("找不到待同步的 ERP 交換資料。")
                        resource_version = exchange_resource_version(record)
                        policy_version = ERP_EXCHANGE_POLICY_VERSION
                        expected_operation_id = build_exchange_operation_id(
                            source_system, external_id, record["version"]
                        )
                        if not hmac.compare_digest(
                            operation_id, expected_operation_id
                        ):
                            raise ValueError(
                                "operation_id 與 ERP 交換資料版本不相符。"
                            )

                    replay = _replay_protected_operation(
                        operation_id,
                        args,
                        expected_tool=tool_name,
                        expected_resource_version=resource_version,
                        expected_policy_version=policy_version,
                        requester_username=actor,
                    )
                    if replay is not None:
                        return replay

                    if tool_name == "create_purchase_order":
                        from backend.database import run_query

                        if run_query(
                            "SELECT 1 FROM purchase_orders WHERE po_id = ? LIMIT 1",
                            (po_id,),
                        ):
                            raise ValueError(f"採購單號 {po_id} 已存在。")

                approval_id = _create_pending_approval(
                    tool_name,
                    args,
                    role,
                    requester_username=actor,
                    operation_id=operation_id,
                    resource_version=resource_version,
                    policy_version=policy_version,
                )
                if protected_po:
                    replay = _replay_protected_operation(
                        operation_id,
                        args,
                        expected_tool=tool_name,
                        expected_resource_version=resource_version,
                        expected_policy_version=policy_version,
                        requester_username=actor,
                    )
                    if replay is None:
                        raise RuntimeError("審批單建立後無法讀回。")
                    if replay.status != "pending":
                        return replay
            except Exception as exc:
                msg = f"建立審批單失敗：{exc}"
                _write_log(tool_name, args, role, msg, success=False)
                return GatewayResult(status="error", message=msg)
            msg = (
                f"工具「{tool_name}」為寫入操作，已建立審批單（{approval_id}），"
                f"請管理者至 Dashboard 核准後才會執行。"
            )
            _write_log(tool_name, args, role, msg, success=True)
            return GatewayResult(status="pending", message=msg, approval_id=approval_id)

        elif risk_level == "dangerous":
            # 攔截，送審批（並加警告）
            try:
                approval_id = _create_pending_approval(
                    tool_name,
                    args,
                    role,
                    requester_username=actor,
                    operation_id=operation_id,
                )
            except Exception as exc:
                msg = f"建立審批單失敗：{exc}"
                _write_log(tool_name, args, role, msg, success=False)
                return GatewayResult(status="error", message=msg)
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

    def approve_action(self, approval_id: str, approver: str) -> GatewayResult:
        """
        核准審批項目，並真正執行該工具操作。
        """
        from backend.agent_logger import (
            get_pending_approval_by_id,
            transition_approval_status,
        )
        
        item = get_pending_approval_by_id(approval_id)
        if not item:
            return GatewayResult(status="error", message=f"找不到 ID 為 {approval_id} 的審批項目。")

        if item["tool_name"] == "create_purchase_order":
            return self._approve_purchase_order(approval_id, approver)
        if item["tool_name"] == "sync_external_purchase_order":
            return self._approve_purchase_order(
                approval_id,
                approver,
                expected_tool="sync_external_purchase_order",
            )

        from backend.access_control import (
            GLOBAL_APPROVAL_DECIDE,
            load_principal,
        )

        approver_principal = load_principal(approver)
        if (
            approver_principal is None
            or not approver_principal.can(GLOBAL_APPROVAL_DECIDE)
        ):
            return GatewayResult(
                status="denied",
                message="核准者沒有處理全域審批項目的權限。",
            )
        approver_username = approver_principal.username

        requester_username = str(item.get("requester_username") or "").strip()
        if not requester_username:
            return GatewayResult(
                status="denied",
                message=(
                    "此舊版審批缺少可驗證的提案人，不能核准；"
                    "請拒絕後由已登入使用者重新送審。"
                ),
                approval_id=approval_id,
            )
        if hmac.compare_digest(requester_username, approver_username):
            return GatewayResult(
                status="denied",
                message="提案人不得核准自己的提案。",
                approval_id=approval_id,
            )
            
        if item["status"] != "pending":
            return GatewayResult(status="error", message=f"該審批項目的狀態為 {item['status']}，無法重複核准。")
            
        tool_name = item["tool_name"]
        args = item["parameters"]
        role = item["requester"]
        
        # 先用 CAS 取得唯一執行權，避免雙擊或兩個工作階段重複執行。
        # 舊版工具尚未全面支援共用 DB connection；若效果完成後終態落庫失敗，
        # 狀態會保留 executing 並要求人工對帳，不會自動重試。
        claimed = transition_approval_status(
            approval_id,
            expected_status="pending",
            expected_version=item["version"],
            new_status="executing",
            approver=approver_username,
        )
        if not claimed:
            return GatewayResult(
                status="error",
                message="審批狀態已被其他操作更新，未取得執行權。",
                approval_id=approval_id,
            )

        result_gateway = self._execute(tool_name, args, role)
        if result_gateway.status != "ok":
            transition_approval_status(
                approval_id,
                expected_status="executing",
                expected_version=item["version"] + 1,
                new_status="failed",
                approver=approver_username,
                reason=result_gateway.message,
            )
            result_gateway.approval_id = approval_id
            return result_gateway

        from backend.database import transaction

        receipt_operation_id = str(item.get("operation_id") or "").strip()
        if not receipt_operation_id:
            receipt_operation_id = f"generic-approval:{approval_id}"
        receipt_digest = str(item.get("payload_digest") or "").strip()
        if not receipt_digest:
            receipt_digest = canonical_payload_digest(
                tool_name=tool_name,
                args=args,
                resource_version=str(item.get("resource_version") or "unspecified"),
                policy_version=str(item.get("policy_version") or "generic-approval-v1"),
                requester_username=requester_username,
            )
        try:
            result_json = json.dumps(
                result_gateway.data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
                default=str,
            )
            with transaction(immediate=True) as conn:
                conn.execute(
                    """
                    INSERT INTO effect_receipts (
                        operation_id, approval_id, payload_digest, result,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_operation_id,
                        approval_id,
                        receipt_digest,
                        result_json,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                transitioned = transition_approval_status(
                    approval_id,
                    expected_status="executing",
                    expected_version=item["version"] + 1,
                    new_status="approved",
                    approver=approver_username,
                    conn=conn,
                )
                if not transitioned:
                    raise RuntimeError("無法寫入審批終態。")
        except Exception as exc:
            msg = (
                "工具效果已執行，但執行收據或審批終態寫入失敗；"
                f"請人工對帳，系統不會自動重試：{exc}"
            )
            _write_log(
                tool_name,
                {"approval_id": approval_id},
                approver_username,
                msg,
                success=False,
            )
            return GatewayResult(
                status="error",
                message=msg,
                approval_id=approval_id,
            )
        result_gateway.approval_id = approval_id
        return result_gateway

    def _approve_purchase_order(
        self,
        approval_id: str,
        approver: str,
        *,
        expected_tool: str = "create_purchase_order",
    ) -> GatewayResult:
        """Commit a protected PO effect, receipt, and final state atomically."""
        from backend.agent_logger import (
            _PROTECTED_APPROVAL_CONTEXT,
            transition_approval_status,
            write_action_log,
        )
        from backend.database import transaction
        from backend.procurement import _PO_APPROVAL_CONTEXT
        from backend.erp_exchange import (
            _ERP_EXCHANGE_APPROVAL_CONTEXT,
            exchange_resource_version,
            get_exchange_record,
        )

        failure_args = {"approval_id": approval_id}
        approver_username = str(approver or "").strip()
        try:
            with transaction(immediate=True) as conn:
                conn.row_factory = sqlite3.Row
                item = conn.execute(
                    """
                    SELECT approval_id, tool_name, parameters, requester, status,
                           requester_username, approver, operation_id, payload_digest,
                           resource_version, policy_version, version
                    FROM pending_approvals
                    WHERE approval_id = ?
                    """,
                    (approval_id,),
                ).fetchone()
                if item is None:
                    return GatewayResult(
                        status="error",
                        message=f"找不到 ID 為 {approval_id} 的審批項目。",
                    )
                if item["tool_name"] != expected_tool:
                    return GatewayResult(
                        status="error", message="審批單工具類型不相符。"
                    )

                from backend.access_control import (
                    APPROVAL_DECIDE,
                    load_principal,
                )

                approver_principal = load_principal(approver, conn=conn)
                if (
                    approver_principal is None
                    or not approver_principal.can(APPROVAL_DECIDE)
                ):
                    return GatewayResult(
                        status="denied",
                        message="核准者目前不具審批權限，操作未執行。",
                    )
                approver_username = approver_principal.username
                requester_username = str(
                    item["requester_username"] or ""
                ).strip()
                if not requester_username:
                    return GatewayResult(
                        status="denied",
                        message="審批單缺少可驗證的提案人，操作未執行。",
                    )
                if hmac.compare_digest(requester_username, approver_username):
                    return GatewayResult(
                        status="denied",
                        message="提案人不得核准自己的操作。",
                    )

                operation_id = item["operation_id"]
                failure_args["operation_id"] = operation_id
                stored_digest = item["payload_digest"]
                resource_version = item["resource_version"]
                policy_version = item["policy_version"]
                if not all(
                    (
                        operation_id,
                        stored_digest,
                        resource_version,
                        policy_version,
                    )
                ):
                    return GatewayResult(
                        status="error", message="審批單缺少必要的完整性欄位。"
                    )
                expected_policy_version = (
                    PO_APPROVAL_POLICY_VERSION
                    if expected_tool == "create_purchase_order"
                    else ERP_EXCHANGE_POLICY_VERSION
                )
                if policy_version != expected_policy_version:
                    return GatewayResult(
                        status="error", message="審批政策版本不受支援，請重新送審。"
                    )
                if expected_tool == "create_purchase_order":
                    if resource_version != "absent":
                        return GatewayResult(
                            status="error", message="採購單資源版本已失效，請重新送審。"
                        )

                try:
                    args = json.loads(item["parameters"])
                except (TypeError, json.JSONDecodeError):
                    return GatewayResult(
                        status="error", message="審批參數格式已損壞，操作未執行。"
                    )
                if not isinstance(args, dict):
                    return GatewayResult(
                        status="error", message="審批參數格式不合法，操作未執行。"
                    )

                computed_digest = canonical_payload_digest(
                    tool_name=item["tool_name"],
                    args=args,
                    resource_version=resource_version,
                    policy_version=policy_version,
                    requester_username=requester_username,
                )
                if not hmac.compare_digest(stored_digest, computed_digest):
                    return GatewayResult(
                        status="error", message="審批內容完整性驗證失敗，操作未執行。"
                    )

                receipt = conn.execute(
                    """
                    SELECT result, approval_id, payload_digest
                    FROM effect_receipts
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if item["status"] != "approved":
                        return GatewayResult(
                            status="error",
                            message="執行收據與審批終態不一致。",
                        )
                    if (
                        receipt["approval_id"] != approval_id
                        or not hmac.compare_digest(
                            receipt["payload_digest"], stored_digest
                        )
                    ):
                        return GatewayResult(
                            status="error",
                            message="既有執行收據與審批內容不一致。",
                        )
                    return GatewayResult(
                        status="ok", data=json.loads(receipt["result"])
                    )

                if expected_tool == "sync_external_purchase_order":
                    staged = get_exchange_record(
                        str(args.get("source_system") or ""),
                        str(args.get("external_id") or ""),
                        conn=conn,
                    )
                    if staged is None or not hmac.compare_digest(
                        resource_version, exchange_resource_version(staged)
                    ):
                        return GatewayResult(
                            status="error",
                            message="ERP 交換資料版本已失效，請重新送審。",
                        )

                if item["status"] != "pending":
                    return GatewayResult(
                        status="error",
                        message=f"該審批項目的狀態為 {item['status']}，無法核准。",
                    )

                if expected_tool == "create_purchase_order":
                    po_id = str(args.get("po_id") or "").strip()
                    if conn.execute(
                        "SELECT 1 FROM purchase_orders WHERE po_id = ? LIMIT 1",
                        (po_id,),
                    ).fetchone():
                        return GatewayResult(
                            status="error",
                            message="採購單資源已存在，原核准內容已過期。",
                        )

                start_version = int(item["version"])
                if not transition_approval_status(
                    approval_id,
                    expected_status="pending",
                    expected_version=start_version,
                    new_status="executing",
                    approver=approver_username,
                    conn=conn,
                    approval_context=_PROTECTED_APPROVAL_CONTEXT,
                ):
                    raise RuntimeError("審批狀態競態，未取得執行權。")

                if expected_tool == "create_purchase_order":
                    result = tools_mapping[expected_tool](
                        **args,
                        _conn=conn,
                        _operation_id=operation_id,
                        _approval_context=_PO_APPROVAL_CONTEXT,
                    )
                else:
                    result = tools_mapping[expected_tool](
                        **args,
                        _conn=conn,
                        _operation_id=operation_id,
                        _resource_version=resource_version,
                        _approval_context=_ERP_EXCHANGE_APPROVAL_CONTEXT,
                    )
                result_json = json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                conn.execute(
                    """
                    INSERT INTO effect_receipts (
                        operation_id, approval_id, payload_digest, result,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        approval_id,
                        stored_digest,
                        result_json,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                write_action_log(
                    item["tool_name"],
                    args,
                    item["requester"],
                    result_json,
                    True,
                    conn=conn,
                )
                if not transition_approval_status(
                    approval_id,
                    expected_status="executing",
                    expected_version=start_version + 1,
                    new_status="approved",
                    approver=approver_username,
                    conn=conn,
                    approval_context=_PROTECTED_APPROVAL_CONTEXT,
                ):
                    raise RuntimeError("無法寫入審批終態。")
                return GatewayResult(status="ok", data=result)
        except Exception as exc:
            msg = f"受保護採購單執行失敗：{exc}"
            _write_log(
                expected_tool,
                failure_args,
                approver_username,
                msg,
                success=False,
            )
            return GatewayResult(status="error", message=msg)

    def reject_action(self, approval_id: str, reason: str, approver: str) -> GatewayResult:
        """
        拒絕審批項目，操作作廢並記錄原因。
        """
        from backend.agent_logger import (
            get_pending_approval_by_id,
            transition_approval_status,
            write_action_log,
        )
        
        item = get_pending_approval_by_id(approval_id)
        if not item:
            return GatewayResult(status="error", message=f"找不到 ID 為 {approval_id} 的審批項目。")
            
        if item["tool_name"] in {
            "create_purchase_order",
            "sync_external_purchase_order",
        }:
            return self._reject_purchase_order(approval_id, reason, approver)

        from backend.access_control import (
            GLOBAL_APPROVAL_DECIDE,
            load_principal,
        )

        approver_principal = load_principal(approver)
        if (
            approver_principal is None
            or not approver_principal.can(GLOBAL_APPROVAL_DECIDE)
        ):
            return GatewayResult(
                status="denied",
                message="拒絕者沒有處理全域審批項目的權限。",
            )
        approver_username = approver_principal.username

        if item["status"] != "pending":
            return GatewayResult(status="error", message=f"該審批項目的狀態為 {item['status']}，無法重複拒絕。")
            
        tool_name = item["tool_name"]
        args = item["parameters"]
        role = item["requester"]

        requester_username = str(item.get("requester_username") or "").strip()
        if requester_username and hmac.compare_digest(
            requester_username, approver_username
        ):
            return GatewayResult(
                status="denied",
                message="提案人不得拒絕自己的提案。",
                approval_id=approval_id,
            )
        if not requester_username:
            reason = f"[legacy originator unavailable] {reason}"
        
        # 將狀態更新為 rejected 並存入拒絕原因
        if not transition_approval_status(
            approval_id,
            expected_status="pending",
            expected_version=item["version"],
            new_status="rejected",
            approver=approver_username,
            reason=reason,
        ):
            return GatewayResult(
                status="error", message="審批狀態已被其他操作更新。"
            )
        
        # 記錄作廢日誌，包含原因
        msg = f"操作遭管理者「{approver_username}」拒絕，原因：{reason}。該工具執行已作廢。"
        write_action_log(tool_name, args, role, msg, success=False)
        
        return GatewayResult(status="denied", message=msg)

    def _reject_purchase_order(
        self, approval_id: str, reason: str, approver: str
    ) -> GatewayResult:
        from backend.agent_logger import (
            _PROTECTED_APPROVAL_CONTEXT,
            transition_approval_status,
            write_action_log,
        )
        from backend.database import transaction

        approver_username = str(approver or "").strip()
        try:
            with transaction(immediate=True) as conn:
                row = conn.execute(
                    """
                    SELECT tool_name, parameters, requester, status, version,
                           requester_username
                    FROM pending_approvals WHERE approval_id = ?
                    """,
                    (approval_id,),
                ).fetchone()
                if row is None:
                    return GatewayResult(
                        status="error",
                        message=f"找不到 ID 為 {approval_id} 的審批項目。",
                    )
                if row[0] not in {
                    "create_purchase_order",
                    "sync_external_purchase_order",
                }:
                    return GatewayResult(
                        status="error", message="審批單工具類型不相符。"
                    )
                from backend.access_control import (
                    APPROVAL_DECIDE,
                    load_principal,
                )

                approver_principal = load_principal(approver, conn=conn)
                if (
                    approver_principal is None
                    or not approver_principal.can(APPROVAL_DECIDE)
                ):
                    return GatewayResult(
                        status="denied", message="拒絕者目前不具審批權限。"
                    )
                approver_username = approver_principal.username
                requester_username = str(row[5] or "").strip()
                legacy_originator_missing = not requester_username
                if requester_username and hmac.compare_digest(
                    requester_username, approver_username
                ):
                    return GatewayResult(
                        status="denied", message="提案人不得拒絕自己的操作。"
                    )
                if row[3] != "pending":
                    return GatewayResult(
                        status="error",
                        message=f"該審批項目的狀態為 {row[3]}，無法拒絕。",
                    )
                recorded_reason = (
                    f"[legacy originator unavailable] {reason}"
                    if legacy_originator_missing
                    else reason
                )
                if not transition_approval_status(
                    approval_id,
                    expected_status="pending",
                    expected_version=row[4],
                    new_status="rejected",
                    approver=approver_username,
                    reason=recorded_reason,
                    conn=conn,
                    approval_context=_PROTECTED_APPROVAL_CONTEXT,
                ):
                    raise RuntimeError("審批狀態競態，拒絕未生效。")
                args = json.loads(row[1])
                msg = (
                    f"操作遭管理者「{approver_username}」拒絕，原因：{recorded_reason}。"
                    "該工具執行已作廢。"
                )
                write_action_log(
                    row[0], args, row[2], msg, success=False, conn=conn
                )
                return GatewayResult(status="denied", message=msg)
        except Exception as exc:
            return GatewayResult(
                status="error", message=f"拒絕審批失敗：{exc}"
            )

    def execute_approved(self, tool_name: str, args: dict, role: str = "admin") -> GatewayResult:
        """
        僅供已人工核准的補償操作直接執行（跳過 write 攔截），並寫入 action log。
        適用場景：Dashboard 沖銷/重試，管理員已人工確認，不需再送審批。
        """
        if tool_name in {
            "create_purchase_order",
            "sync_external_purchase_order",
        }:
            return GatewayResult(
                status="denied",
                message="受保護採購操作不得繞過 operation-bound 人工審批。",
            )
        return self._execute(tool_name, args, role)


# 全域單例，直接 import 使用
gateway = ToolGateway()
