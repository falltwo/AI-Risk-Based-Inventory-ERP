"""
backend/scenario_contract.py
單一 SKU / 單一 PO 的情境資料契約（dataclass），供 scenario_engine 與測試共用。

獨立研究模組：只依賴 Python 標準函式庫，不 import 任何 Streamlit / ERP 模組，
以便在隔離 venv 中與 Stockpyl 一起重跑（對應 vault `19-學術專題開源整合候選重查.md:95-130`）。

單一 PO 契約語意（GPT-6 CHANGES_REQUIRED 修正後）：
  - 契約必須明示 po_id、po_quantity（>0）與 arrival timing（lead_time_days，整數天）。
  - 整個 horizon 內只允許該 PO「一次」到貨（到貨期 t=lead_time_days）；不是無限供應、
    也不是每期補貨的 base-stock policy。
  - lead_time_days 非整數 → 共同 validate 層 fail-closed（needs_input），不得 round()。

共同 validate 層（第二修正回合，GPT-6 最終 verdict #5）：
  - 型別、有限性、整數語意與既定值域在引擎前統一攔截；validate() 本身不得拋
    未捕捉 TypeError/OverflowError。
  - horizon_days 必須是 int（>0）：明確拒絕 float（如 20.0）與 bool。
  - po_quantity／initial_stock 必須是有限數值（NaN/Infinity/字串 → needs_input）。
  - demand_list 逐項檢查：每項必須是有限非負數值（None/NaN/負數 → needs_input）。

注意：一般 `import backend.scenario_*` 仍會先執行既有 backend/__init__.py（載入既有
ERP 模組）；本模組本身未新增任何 ERP 依賴，測試以 repo venv / 隔離 venv 分層執行。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

SCENARIO_ENGINE_VERSION = "1.3.0"
# 1.0.0=修正前（base-stock 語意，GPT-6 否決）；1.1.0=單一 PO 語意；
# 1.2.0=第二修正回合：comparator 非有限值 fail-closed、共同 validate 層型別/有限性/值域攔截、
#        Stockpyl adapter policy 訂單與 override 同步 + 逐期簿記（order_bookkeeping）。
# 1.3.0=第四修正回合（reviewer 獨立重現之剩餘缺口）：comparator 容器契約（inventory_levels 只
#        接受數值 list、po_arrivals 驗證容器/二欄/期數/有限數量）、validation 安全訊息
#        （_safe_repr，不因超大 int repr 拋例外）、運算後非有限 fail-closed（numerical failure）、
#        失敗輸入可保存（rejected 標記、標準 JSON）。
NUMERIC_TOLERANCE = 1e-9


@dataclass
class DemandSpec:
    type: str | None = None
    demand_list: list[float] | None = None
    mean: float | None = None
    standard_deviation: float | None = None


@dataclass
class Validation:
    ok: bool = True
    needs_input: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _is_real_number(x) -> bool:
    """int/float 且非 bool（bool 是 int 子類，契約上不算數值）。"""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_finite_number(x) -> bool:
    """有限數值（排除 NaN/Infinity、字串、None、bool、超出 float 範圍的超大 int）。

    超大 int（如 10**10000）→ math.isfinite/float() 會 OverflowError，先轉 float
    並捕捉；不合約值一律回 False，不得拋例外（第三修正回合，父 Agent 探針反例）。
    """
    if not _is_real_number(x):
        return False
    try:
        return math.isfinite(float(x))
    except (OverflowError, ValueError):
        return False


def _safe_repr(x) -> str:
    """有界、絕不拋例外的值描述（第四修正回合）。

    驗證訊息／差異紀錄嵌入使用者提供值時一律用本函式，不用 {x!r}：
    repr 超大 int（如 10**10000）會觸發 int_max_str_digits 的 ValueError，
    讓「建構錯誤訊息」本身再度崩潰。repr 失敗時回型別/位元數描述，不提高全域限制。
    """
    try:
        r = repr(x)
    except Exception:
        if isinstance(x, int):
            try:
                return f"<int: {x.bit_length()} bits（超出字串轉換上限）>"
            except Exception:
                return "<int: 超出字串轉換上限>"
        return f"<{type(x).__name__}: 不可安全表示>"
    if len(r) > 200:
        r = r[:200] + "…"
    return r


def _is_whole_number(x) -> bool:
    """整數值數字（int 或整數值 float，如 2 / 2.0）；1.5 → False。

    非有限值（inf/nan）→ False（不在 inf 上呼叫 is_integer()，避免 OverflowError）。
    """
    if isinstance(x, bool):
        return False
    if isinstance(x, int):
        return True
    if isinstance(x, float):
        return math.isfinite(x) and x.is_integer()
    return False


@dataclass
class ScenarioInput:
    case_id: str | None = None
    scenario_id: str | None = None
    input_version: int | None = None
    seed: int = 0
    horizon_days: int | None = None
    sku: str | None = None
    po_id: str | None = None
    po_quantity: float | None = None
    initial_stock: float | None = None
    lead_time_days: float | None = None
    holding_cost: float | None = None
    stockout_cost: float | None = None
    demand: DemandSpec | None = None
    assumption_source: str | None = None
    description: str | None = None

    def validate(self) -> Validation:
        """fail-closed 驗證：缺必要輸入或輸入型別/有限性/值域不合約 → needs_input。

        單一 PO 契約必要輸入：horizon_days（int，>0）、po_id（非空字串）、
        po_quantity（有限數值，>0）、initial_stock（有限數值，>=0）、
        lead_time_days（有限整數值且 0 <= lead < horizon_days，即到貨期必須落在
        horizon 內）、demand（type=D 時 demand_list 長度==horizon 且逐項有限非負）。
        成本為選填，但若提供必須是有限數值且 >= 0；缺成本不回傳推測成本。

        本方法對任何輸入型別不得拋例外：所有檢查都先做型別/有限性判斷再比較。
        """
        v = Validation()

        def _need(field: str, issue: str) -> None:
            v.needs_input.append(field)
            v.issues.append(issue)

        horizon_ok = isinstance(self.horizon_days, int) and not isinstance(self.horizon_days, bool)

        # horizon_days：必須是 int 且 > 0（明確拒絕 float，如 20.0；引擎以 range(horizon) 使用）
        if not horizon_ok or self.horizon_days <= 0:
            _need("horizon_days", "horizon_days 必須是正整數 int（不是 float/bool）")

        # po_id：非空字串（單一 PO 契約的必要識別）
        if not self.po_id or not isinstance(self.po_id, str):
            _need("po_id", "po_id 必須是非空字串（單一 PO 契約的必要識別）")

        # po_quantity：有限數值且 > 0（NaN/Infinity/字串/None/負/0 → needs_input）
        if self.po_quantity is None or not _is_finite_number(self.po_quantity) or self.po_quantity <= 0:
            _need("po_quantity", "po_quantity 未提供、非有限數值或 <= 0（單一 PO 契約須明示 PO 數量）")

        # initial_stock：有限數值且 >= 0
        if self.initial_stock is None or not _is_finite_number(self.initial_stock) or self.initial_stock < 0:
            _need("initial_stock", "initial_stock 未提供、非有限數值或為負")

        # lead_time_days：有限數值、整數值、>= 0；型別檢查先於比較，不拋 TypeError
        if (
            self.lead_time_days is None
            or not _is_finite_number(self.lead_time_days)
            or not _is_whole_number(self.lead_time_days)
            or self.lead_time_days < 0
        ):
            _need(
                "lead_time_days",
                "lead_time_days 未提供、非數值/非有限、為負或非整數值（到貨 timing 以整天計，fail-closed，不 round）",
            )
        elif horizon_ok and self.lead_time_days >= self.horizon_days:
            _need("lead_time_days", f"lead_time_days={_safe_repr(self.lead_time_days)} 不在 horizon 內（單張 PO 無法到貨）")

        # 成本（選填）：若提供必須是有限數值且 >= 0；缺成本不回傳推測成本
        if self.holding_cost is not None and (not _is_finite_number(self.holding_cost) or self.holding_cost < 0):
            _need("holding_cost", "holding_cost 若提供必須是有限數值且 >= 0")
        if self.stockout_cost is not None and (not _is_finite_number(self.stockout_cost) or self.stockout_cost < 0):
            _need("stockout_cost", "stockout_cost 若提供必須是有限數值且 >= 0")

        # demand：先驗容器/型別，再存取 .type、len() 或迭代；錯型別回 needs_input，不拋例外
        d = self.demand
        if not isinstance(d, DemandSpec) or d.type is None:
            _need("demand", "demand 未提供或不是 DemandSpec（或 type 缺失）")
        elif d.type == "D":
            if not isinstance(d.demand_list, list) or not d.demand_list:
                _need("demand", "demand type=D 但 demand_list 缺失或非 list 容器")
            else:
                if horizon_ok and len(d.demand_list) != self.horizon_days:
                    _need("demand", f"demand_list 長度 {len(d.demand_list)} != horizon_days {self.horizon_days}")
                for i, x in enumerate(d.demand_list):
                    if x is None or not _is_finite_number(x) or x < 0:
                        # 用 _safe_repr：超大 int 的 repr 會觸發 int_max_str_digits 上限，
                        # 錯誤訊息建構本身不得再度崩潰（第四修正回合，reviewer 反例）。
                        _need("demand", f"demand_list[{i}]={_safe_repr(x)} 必須是有限非負數值（逐項檢查）")
        elif d.type == "N":
            if d.mean is None or not _is_finite_number(d.mean):
                _need("demand", "demand type=N 但 mean 缺失或非有限數值")
            if (
                d.standard_deviation is None
                or not _is_finite_number(d.standard_deviation)
                or d.standard_deviation < 0
            ):
                _need("demand", "demand type=N 但 standard_deviation 缺失、非有限數值或為負")
        else:
            _need("demand", f"未知 demand type: {_safe_repr(d.type)}")
        # 去重、保持順序
        v.needs_input = list(dict.fromkeys(v.needs_input))
        v.ok = not v.needs_input
        return v


@dataclass
class ScenarioResult:
    engine: str = ""
    engine_version: str = SCENARIO_ENGINE_VERSION
    stockpyl_version: str | None = None
    case_id: str | None = None
    scenario_id: str | None = None
    input_version: int | None = None
    seed: int = 0
    horizon_days: int | None = None
    po_id: str | None = None
    po_quantity: float | None = None
    po_arrivals: list = field(default_factory=list)  # [[到貨期, 數量], ...]；單一 PO 契約下至多一筆
    order_bookkeeping: list | None = None
    # 僅 Stockpyl adapter 填寫：逐期 [{"t", "order_quantity_fg", "pending_finished_goods",
    # "raw_material_inventory"}]；用於證明 policy 決策與單一 PO override 同步、無殘留訂單簿記。
    # baseline 引擎為 None。
    inventory_levels: list[float] = field(default_factory=list)
    fill_rate: float | None = None
    first_stockout_period: int | None = None
    stockout_periods: int | None = None
    mean_cost_per_day: float | None = None
    holding_cost: float | None = None
    stockout_cost: float | None = None
    total_cost: float | None = None
    costs_provided: bool = False
    needs_input: list[str] = field(default_factory=list)
    assumptions: list[dict] = field(default_factory=list)
    demand_total_realized: float | None = None
    executed_at: str | None = None
    execution_time_seconds: float | None = None

    def canonical_bytes(self) -> bytes:
        """canonical structured result：固定欄位、固定精度、排除執行時間等非數值 metadata。

        - 小數固定 12 位（round），-0.0 正規化為 0.0，避免平台 repr 差異。
        - 排除 executed_at / execution_time_seconds（acceptance #1）。
        - po_id / po_quantity / po_arrivals / order_bookkeeping 為單一 PO 契約與簿記
          證據的一部分，納入 canonical（po_id 參與輸出語意，不是無效欄位）。
        - allow_nan=False：引擎在契約層已拒絕非有限輸入，canonical 遇 NaN/Infinity
          會直接 ValueError（fail-closed，不產出假一致的 bytes）。
        """
        def _canon(x):
            if x is None:
                return None
            if isinstance(x, bool):
                return x
            r = round(float(x), 12)
            return 0.0 if r == 0 else r

        payload = {
            "engine": self.engine,
            "engine_version": self.engine_version,
            "stockpyl_version": self.stockpyl_version,
            "case_id": self.case_id,
            "scenario_id": self.scenario_id,
            "input_version": self.input_version,
            "seed": self.seed,
            "horizon_days": self.horizon_days,
            "po_id": self.po_id,
            "po_quantity": _canon(self.po_quantity),
            "po_arrivals": [[int(t), _canon(q)] for t, q in self.po_arrivals],
            "order_bookkeeping": (
                None
                if self.order_bookkeeping is None
                else [
                    [
                        int(e["t"]),
                        _canon(e["order_quantity_fg"]),
                        _canon(e["pending_finished_goods"]),
                        _canon(e["raw_material_inventory"]),
                    ]
                    for e in self.order_bookkeeping
                ]
            ),
            "inventory_levels": [_canon(x) for x in self.inventory_levels],
            "fill_rate": _canon(self.fill_rate),
            "first_stockout_period": self.first_stockout_period,
            "stockout_periods": self.stockout_periods,
            "mean_cost_per_day": _canon(self.mean_cost_per_day),
            "holding_cost": _canon(self.holding_cost),
            "stockout_cost": _canon(self.stockout_cost),
            "total_cost": _canon(self.total_cost),
            "costs_provided": self.costs_provided,
            "needs_input": list(self.needs_input),
            "assumptions": self.assumptions,
            "demand_total_realized": _canon(self.demand_total_realized),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass
class ScenarioRun:
    inputs: ScenarioInput | None = None
    baseline: ScenarioResult | None = None
    stockpyl: ScenarioResult | None = None
    explanation: str | None = None
    failures: list[str] = field(default_factory=list)
    engine_version: str = SCENARIO_ENGINE_VERSION
    stockpyl_version: str | None = None
