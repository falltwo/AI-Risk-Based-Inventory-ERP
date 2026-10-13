"""
backend/scenario_engine.py
情境引擎：deterministic baseline + Stockpyl adapter + 執行 / 差異 / 保存。

設計原則（vault `19-學術專題開源整合候選重查.md:95-140`、`18-2026產品功能與技術架構重估.md:150-156`）：
  - LLM 只能讀取「深拷貝快照」產生文字解釋，不得生成或覆寫任何數值欄位。
  - 缺少需求分布、PO 數量、到貨 timing 或交期非整數 → fail closed（needs_input），不 round。
  - Stockpyl 採 lazy import，避免未安裝 Stockpyl 時連 baseline 都不能用。
  - 只處理單一 SKU / 單一 PO；不做多階 BOM、MCP、DBOS、前端。

單一 PO 語意（2026-09-12 第一修正回合，GPT-6 verdict #3；2026-09-13 第二修正回合）：
  - 契約明示 po_quantity（Q）與 arrival timing（lead_time_days，整數天）。
  - baseline：I0 起算，t=lead 到貨 Q（先補 backorder、餘量入現貨），之後不再有任何補貨；
    每期先到貨 → 再遇需求 d_t → 期末 IL_t。
  - Stockpyl 1.0.2 對應（唯讀 API/state_vars 檢查 + spike 實測）：
    sim.initialize + 逐期 sim.step(order_quantity_override=...) + sim.close（公開 API，
    文件說明供外部 RL 控制逐期訂單）；外部供應節點 shipment_lead_time=lead、
    order_lead_time=0；t=0 override 下唯一一筆 Q，其餘期 override=0。
    FG 訂單由 policy 產生（override 只覆寫 RM 採購），故以 rQ reorder_point 逐期
    切換 +inf/−inf 使 policy 決策與 override 同步（第二修正回合，詳見
    stockpyl_simulation docstring 與 docs/research/scenario_validation.md §4）。
  - 第二修正回合新增：comparator 非有限值 fail-closed、共同 validate 層型別/有限性/
    值域攔截、adapter 逐期簿記（order_bookkeeping）與映射後置條件 fail-closed 檢查。
  - 第四修正回合（1.3.0）新增：comparator 容器契約（inventory_levels 只接受數值
    list、po_arrivals 驗證容器/二欄/期數/有限數量）、_safe_repr 安全訊息、
    _assert_result_finite 運算後非有限後置條件、rejected 標記的標準 JSON 保存。
"""
from __future__ import annotations

import copy
import datetime
import json
import math
import pathlib
import re
import time

from .scenario_contract import (
    NUMERIC_TOLERANCE,
    SCENARIO_ENGINE_VERSION,
    DemandSpec,
    ScenarioInput,
    ScenarioResult,
    ScenarioRun,
    _is_finite_number,
    _is_whole_number,
    _safe_repr,
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


class LLMAdapter:
    """LLM 解釋 adapter 介面：只回傳文字解釋，不得回傳數值。"""

    def explain(self, run: ScenarioRun) -> str:  # pragma: no cover - 介面
        raise NotImplementedError


class FailingLLMAdapter(LLMAdapter):
    """只要被呼叫就失敗的 adapter（acceptance #4）。"""

    def explain(self, run: ScenarioRun) -> str:
        raise RuntimeError("LLM disabled by test: numeric results must still be produced")


def get_stockpyl_version() -> str:
    try:
        import importlib.metadata

        return importlib.metadata.version("stockpyl")
    except Exception:
        return "unavailable"


def _assumptions_for(inp: ScenarioInput) -> list[dict]:
    """明示提供的合成假設：標示 assumption=True 與來源（acceptance #5）。

    單一 PO 契約的合成輸入全部列入：demand、lead_time_days、initial_stock、
    po_quantity；成本若提供也列入。
    """
    if not inp.assumption_source:
        return []
    fields = ["demand", "lead_time_days", "initial_stock", "po_quantity"]
    if inp.holding_cost is not None:
        fields.append("holding_cost")
    if inp.stockout_cost is not None:
        fields.append("stockout_cost")
    return [
        {"field": f, "assumption": True, "source": inp.assumption_source}
        for f in fields
    ]


def _empty_result(inp: ScenarioInput, engine: str, needs_input: list[str] | None = None) -> ScenarioResult:
    return ScenarioResult(
        engine=engine,
        case_id=inp.case_id,
        scenario_id=inp.scenario_id,
        input_version=inp.input_version,
        seed=inp.seed,
        horizon_days=inp.horizon_days,
        po_id=inp.po_id,
        # 被拒絕的非有限/不可表示值不帶進結果欄位（失敗結果不應以 nan 外觀回傳）；
        # 原值類型與拒絕原因由 persist 的 inputs rejected 標記保存（第四修正回合）。
        po_quantity=inp.po_quantity if _is_finite_number(inp.po_quantity) else None,
        needs_input=list(needs_input or []),
    )


def _assert_result_finite(res: ScenarioResult, engine: str) -> None:
    """成功結果回傳前的數值有限性後置條件（第四修正回合，reviewer 反例）。

    僅驗證「輸入」有限，不足以保證乘法/累加後的結果有限：
    例 holding_cost=1e308 → holding_total=inf、total_cost=inf。
    任何數值欄位非有限 → 拋 ValueError（numerical failure），由 run_scenario 記入
    failures 且該引擎結果為 None——不得留下 failures=[] 的成功外觀。
    不要求支援天文規模運算，只要求不支援時 fail-closed。
    """
    def _fail(field: str, value) -> None:
        raise ValueError(
            f"numerical failure ({engine}): {field} 為非有限數值 {_safe_repr(value)}，"
            "拒絕回傳成功結果（fail-closed，不支援天文規模運算）"
        )

    for name in (
        "po_quantity",
        "fill_rate",
        "first_stockout_period",
        "stockout_periods",
        "mean_cost_per_day",
        "holding_cost",
        "stockout_cost",
        "total_cost",
        "demand_total_realized",
    ):
        value = getattr(res, name)
        if value is not None and not _is_finite_number(value):
            _fail(name, value)
    for i, x in enumerate(res.inventory_levels):
        if x is None or not _is_finite_number(x):
            _fail(f"inventory_levels[{i}]", x)
    for i, entry in enumerate(res.po_arrivals):
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) != 2
            or not _is_finite_number(entry[0])
            or not _is_finite_number(entry[1])
        ):
            _fail(f"po_arrivals[{i}]", entry)
    if res.order_bookkeeping:
        for i, e in enumerate(res.order_bookkeeping):
            for k in ("order_quantity_fg", "pending_finished_goods", "raw_material_inventory"):
                value = e.get(k)
                if value is not None and not _is_finite_number(value):
                    _fail(f"order_bookkeeping[{i}].{k}", value)


def _single_po_baseline(inp: ScenarioInput) -> ScenarioResult:
    """單一 SKU / 單一 PO 的確定性 baseline（手算可驗算）。

    每期事件順序（與 Stockpyl state_vars 實測對齊）：
      1. 到貨：僅 t == lead 一次，數量 Q；先補 backorder，餘量入現貨。
      2. 需求：met = min(現貨, d_t)；不足部分記為 backorder。
      3. 期末 IL_t = 現貨 - backorder。
    無任何補貨政策（不是 base-stock、不是無限供應）。
    只支援 demand type='D'；成本為選填（缺任一則不回傳推測成本）。
    """
    d = inp.demand
    if d.type != "D" or not d.demand_list:
        raise ValueError("deterministic_baseline 只支援 demand type='D' 且提供 demand_list")

    t0 = time.perf_counter()
    horizon = inp.horizon_days
    lead = int(inp.lead_time_days)  # validate 已保證整數值且 < horizon
    demand = [float(x) for x in d.demand_list]
    costs_provided = inp.holding_cost is not None and inp.stockout_cost is not None
    h = float(inp.holding_cost) if costs_provided else None
    p = float(inp.stockout_cost) if costs_provided else None

    on_hand = float(inp.initial_stock)
    backlog = 0.0
    il = [0.0] * horizon
    met_total = 0.0
    stockout_periods = 0
    first_stockout = None
    holding_total = 0.0
    stockout_total = 0.0
    po_arrivals: list = []

    for t in range(horizon):
        # 1) 到貨（唯一一次）
        if t == lead:
            arrival = float(inp.po_quantity)
            po_arrivals.append([t, arrival])
            fill_backlog = min(backlog, arrival)
            backlog -= fill_backlog
            on_hand += arrival - fill_backlog
        # 2) 需求
        dt = demand[t]
        met = min(on_hand, dt)
        on_hand -= met
        backlog += dt - met
        met_total += met
        # 3) 期末 IL（無任何下單）
        il_t = on_hand - backlog
        il[t] = il_t
        if il_t < 0:
            stockout_periods += 1
            if first_stockout is None:
                first_stockout = t
        if costs_provided:
            holding_total += h * max(il_t, 0.0)
            stockout_total += p * backlog

    total_demand = sum(demand)
    result = ScenarioResult(
        engine="baseline",
        case_id=inp.case_id,
        scenario_id=inp.scenario_id,
        input_version=inp.input_version,
        seed=inp.seed,
        horizon_days=horizon,
        po_id=inp.po_id,
        po_quantity=float(inp.po_quantity),
        po_arrivals=po_arrivals,
        inventory_levels=il,
        fill_rate=(met_total / total_demand) if total_demand > 0 else None,
        first_stockout_period=first_stockout,
        stockout_periods=stockout_periods,
        mean_cost_per_day=((holding_total + stockout_total) / horizon) if costs_provided else None,
        holding_cost=holding_total if costs_provided else None,
        stockout_cost=stockout_total if costs_provided else None,
        total_cost=(holding_total + stockout_total) if costs_provided else None,
        costs_provided=costs_provided,
        demand_total_realized=total_demand,
        assumptions=_assumptions_for(inp),
        executed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        execution_time_seconds=time.perf_counter() - t0,
    )
    _assert_result_finite(result, "baseline")
    return result


def deterministic_baseline(inp: ScenarioInput) -> ScenarioResult:
    v = inp.validate()
    if not v.ok:
        return _empty_result(inp, "baseline", v.needs_input)
    return _single_po_baseline(inp)


def stockpyl_simulation(inp: ScenarioInput) -> ScenarioResult:
    """Stockpyl 單一 SKU / 單一 PO 模擬 adapter（單次到貨，非每期補貨）。

    Stockpyl 1.0.2 對應方式（唯讀檢查 + spike 實測，見 docs fit/gap）：
      - sim.initialize / sim.step(order_quantity_override) / sim.close 手動逐期驅動。
      - t=0 外部供應訂單 override=Q（shipment_lead_time=lead → t=lead 到貨一次），
        其餘期 override=0。
      - FG policy 決策與每期 override 同步（GPT-6 最終 verdict #3）：order_quantity_override
        只覆寫原物料採購，FG 訂單只能由 policy 產生（sim.py 公開原始碼確認）。
        rQ 的觸發條件是 inventory_position <= reorder_point，故逐期切換
        reorder_point=+inf（t=0 必訂 Q）/−inf（t>=1 永不訂），公開屬性 setter。
        這使 t=lead 到貨時 share_frac = order_quantity_fg(t=0)/raw_order(t=0) = 1，
        RM 到貨當期即全數轉為可用 FG——不再用有限常數（1e12）冒充無限，也不留
        每期 FG 殘留訂單（舊實作期末 pending_finished_goods=3800）。
      - 逐期簿記（order_quantity_fg／pending_finished_goods／raw_material_inventory）
        寫入 result.order_bookkeeping；模擬結束做後置條件檢查，映射不成立即
        fail-closed 拋錯（拒絕回傳可能錯位的數值），不靠隱藏上限放行。
    """
    v = inp.validate()
    if not v.ok:
        return _empty_result(inp, "stockpyl", v.needs_input)

    try:
        from stockpyl.demand_source import DemandSource as SPDemandSource
        from stockpyl.policy import Policy as SPPolicy
        from stockpyl.supply_chain_network import SupplyChainNetwork, SupplyChainNode
        import stockpyl.sim as spsim
    except Exception as exc:
        raise RuntimeError(f"Stockpyl unavailable: {exc}") from exc

    t0 = time.perf_counter()
    horizon = inp.horizon_days
    lead = int(inp.lead_time_days)  # validate 已保證整數值
    q = float(inp.po_quantity)
    costs_provided = inp.holding_cost is not None and inp.stockout_cost is not None
    h = float(inp.holding_cost) if inp.holding_cost is not None else 0.0
    p = float(inp.stockout_cost) if inp.stockout_cost is not None else 0.0

    d = inp.demand
    if d.type == "D":
        sp_demand = SPDemandSource(type="D", demand_list=[float(x) for x in d.demand_list])
    elif d.type == "N":
        sp_demand = SPDemandSource(type="N", mean=float(d.mean), standard_deviation=float(d.standard_deviation))
    else:
        raise ValueError(f"Stockpyl adapter 不支援 demand type: {d.type!r}")

    network = SupplyChainNetwork()
    node = SupplyChainNode(
        0,
        network=network,
        local_holding_cost=h,
        stockout_cost=p,
        shipment_lead_time=lead,
        order_lead_time=0,
        demand_source=sp_demand,
        initial_inventory_level=float(inp.initial_stock),
        supply_type="U",
        inventory_policy=SPPolicy(type="rQ", reorder_point=0.0, order_quantity=q),
    )
    network.add_node(node)
    prod = node.product_indices[0]
    policy = node.get_attribute("inventory_policy", product=prod)

    spsim.initialize(network, horizon, rand_seed=int(inp.seed))
    for t in range(horizon):
        # FG policy 訂單與 raw override 逐期同步：t=0 唯一一筆 Q，t>=1 零。
        # rQ 觸發條件為 inventory_position <= reorder_point（policy.py 公開原始碼），
        # +inf=必訂、-inf=永不訂；不使用有限常數（1e12）冒充無限。
        policy.reorder_point = float("inf") if t == 0 else float("-inf")
        spsim.step(
            network,
            order_quantity_override={0: {None: {None: (q if t == 0 else 0.0)}}},
            consistency_checks="W",
        )
    spsim.close(network)

    svs = node.state_vars[:horizon]
    rm_index = node.raw_materials_by_product(prod, return_indices=True, network_BOM=True)[0]
    order_bookkeeping = [
        {
            "t": t,
            "order_quantity_fg": float(sv.order_quantity_fg[prod]),
            "pending_finished_goods": float(sv.pending_finished_goods[prod]),
            "raw_material_inventory": float(sv.raw_material_inventory[rm_index]),
        }
        for t, sv in enumerate(svs)
    ]
    inventory_levels = [float(sv.inventory_level[prod]) for sv in svs]
    first_stockout = None
    stockout_periods = 0
    for t, il_t in enumerate(inventory_levels):
        if il_t < 0:
            stockout_periods += 1
            if first_stockout is None:
                first_stockout = t
    fill_rate = float(svs[-1].fill_rate[prod]) if svs and horizon > 0 else None
    holding_total = sum(float(sv.holding_cost_incurred) for sv in svs)
    stockout_total = sum(float(sv.stockout_cost_incurred) for sv in svs)
    demand_total = float(svs[-1].demand_cumul[prod]) if svs else None

    # 到貨事件：直接從 state_vars.inbound_shipment 逐期觀察（單一 PO → 至多一筆）
    po_arrivals: list = []
    for t, sv in enumerate(svs):
        received = 0.0
        for pred_dict in sv.inbound_shipment.values():
            for qty in pred_dict.values():
                if qty and qty > 0:
                    received += float(qty)
        if received > 0:
            po_arrivals.append([t, received])

    # 後置條件檢查（fail-closed）：單一 PO 映射不成立即拋錯，拒絕回傳可能錯位的數值。
    issues: list[str] = []
    ok_arrival = (
        len(po_arrivals) == 1
        and po_arrivals[0][0] == lead
        and abs(po_arrivals[0][1] - q) <= NUMERIC_TOLERANCE
    )
    if not ok_arrival:
        issues.append(f"到貨事件 {po_arrivals} != 預期單一 PO [[{lead}, {q}]]")
    if any(abs(bk["order_quantity_fg"]) > NUMERIC_TOLERANCE for bk in order_bookkeeping[1:]):
        issues.append("t>0 存在非零 policy FG 訂單（殘留訂單簿記）")
    if order_bookkeeping and abs(order_bookkeeping[-1]["pending_finished_goods"]) > NUMERIC_TOLERANCE:
        issues.append(f"期末 pending_finished_goods 非零: {order_bookkeeping[-1]['pending_finished_goods']}")
    if order_bookkeeping and abs(order_bookkeeping[-1]["raw_material_inventory"]) > NUMERIC_TOLERANCE:
        issues.append(f"期末 raw_material_inventory 非零: {order_bookkeeping[-1]['raw_material_inventory']}")
    if issues:
        raise RuntimeError(
            "Stockpyl 單一 PO 映射後置條件違反（fail-closed，拒絕回傳可能錯位的數值）: "
            + "; ".join(issues)
        )

    result = ScenarioResult(
        engine="stockpyl",
        stockpyl_version=get_stockpyl_version(),
        case_id=inp.case_id,
        scenario_id=inp.scenario_id,
        input_version=inp.input_version,
        seed=inp.seed,
        horizon_days=horizon,
        po_id=inp.po_id,
        po_quantity=q,
        po_arrivals=po_arrivals,
        order_bookkeeping=order_bookkeeping,
        inventory_levels=inventory_levels,
        fill_rate=fill_rate,
        first_stockout_period=first_stockout,
        stockout_periods=stockout_periods,
        mean_cost_per_day=((holding_total + stockout_total) / horizon) if costs_provided else None,
        holding_cost=holding_total if costs_provided else None,
        stockout_cost=stockout_total if costs_provided else None,
        total_cost=(holding_total + stockout_total) if costs_provided else None,
        costs_provided=costs_provided,
        demand_total_realized=demand_total,
        assumptions=_assumptions_for(inp),
        executed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        execution_time_seconds=time.perf_counter() - t0,
    )
    _assert_result_finite(result, "stockpyl")
    return result


_COMPARED_SCALARS = (
    "fill_rate",
    "holding_cost",
    "stockout_cost",
    "total_cost",
    "mean_cost_per_day",
    "stockout_periods",
    "demand_total_realized",
    "first_stockout_period",
)


def compare_results(baseline: ScenarioResult, stockpyl: ScenarioResult) -> dict:
    """結構化差異紀錄：每個可比欄位附 baseline / stockpyl / 絕對差與原因。

    fail-closed 規則（GPT-6 verdict #1 + 最終 verdict #1 + 第三修正回合父 Agent 探針 +
    第四修正回合容器契約，reviewer 重現之反例 A/B）：
      - inventory_levels 只接受契約規定的數值 list；None、mapping（dict）、string 或
        其他任意 iterable → match=False、max_abs_diff=None，reason 明列欄位與原因
        （不得 list(dict) 轉鍵列表、不得拋 TypeError）。
      - inventory_levels 長度不同或任一邊為空 → match=False、max_abs_diff=None
        （不得以 0.0 宣稱一致）。
      - 任何元素對含 NaN／Infinity／不可比較值 → match=False、max_abs_diff=None，
        reason 逐項列出 inventory_levels[i] 與兩側值（max() 不會傳播後續 NaN，
        故必須逐元素先驗證有限性，而不是算完 max 再比容差）。
      - scalar 單邊 None 或非有限/不可比較 → match=False、abs_diff=None，
        reason 必須逐項列出欄位名。
      - po_arrivals：驗證容器（只接受 list；None/其他容器 → mismatch 不拋例外）、
        每筆須為二欄 list [期數, 數量]、期數非負整數值、數量有限數值；任何違反 →
        match=False、reason 列欄位/位置（不宣稱 engines agree）。
    """
    cmp: dict = {"_tolerance": NUMERIC_TOLERANCE}

    def _scalar(name: str, a, b) -> dict:
        if a is None and b is None:
            return {"baseline": None, "stockpyl": None, "abs_diff": None, "match": True}
        if a is None or b is None:
            return {"baseline": a, "stockpyl": b, "abs_diff": None, "match": False}
        if not (_is_finite_number(a) and _is_finite_number(b)):
            return {"baseline": a, "stockpyl": b, "abs_diff": None, "match": False}
        return {"baseline": a, "stockpyl": b, "abs_diff": abs(a - b), "match": abs(a - b) <= NUMERIC_TOLERANCE}

    def _coerce_il(values):
        """inventory_levels → list；只接受契約規定的數值 list，不得把 mapping/string
        等任意 iterable 轉 list（dict 會被轉成鍵列表而假一致）。"""
        if values is None:
            return None, "為 None"
        if not isinstance(values, list):
            return None, f"不合約容器（型別 {type(values).__name__}；契約只接受數值 list）"
        return list(values), None

    a_il, a_il_why = _coerce_il(baseline.inventory_levels)
    b_il, b_il_why = _coerce_il(stockpyl.inventory_levels)
    il_coerce_notes: list = []
    if a_il_why:
        il_coerce_notes.append(f"inventory_levels: baseline {a_il_why}，無法比較")
    if b_il_why:
        il_coerce_notes.append(f"inventory_levels: stockpyl {b_il_why}，無法比較")
    non_finite_pairs: list = []
    max_abs_diff = None
    il_match = False
    if a_il is not None and b_il is not None and len(a_il) == len(b_il) and a_il and b_il:
        for i, (x, y) in enumerate(zip(a_il, b_il)):
            if not (_is_finite_number(x) and _is_finite_number(y)):
                non_finite_pairs.append((i, x, y))
        if non_finite_pairs:
            max_abs_diff = None  # 任何非有限/不可比較 → 不給數值 max_abs_diff
        else:
            max_abs_diff = max(abs(x - y) for x, y in zip(a_il, b_il))
            il_match = max_abs_diff <= NUMERIC_TOLERANCE
    cmp["inventory_levels"] = {
        "baseline": a_il,
        "stockpyl": b_il,
        "max_abs_diff": max_abs_diff,
        "match": il_match,
    }

    for f in _COMPARED_SCALARS:
        cmp[f] = _scalar(f, getattr(baseline, f), getattr(stockpyl, f))

    cmp["po_id"] = {
        "baseline": baseline.po_id,
        "stockpyl": stockpyl.po_id,
        "match": baseline.po_id == stockpyl.po_id,
    }

    # po_arrivals：先驗契約容器與每筆二欄結構，再比較（第四修正回合）。
    def _coerce_arrivals(values, side: str):
        issues: list = []
        if values is None:
            return None, [f"po_arrivals ({side}): 為 None，無法比較"]
        if not isinstance(values, list):
            return None, [
                f"po_arrivals ({side}): 不合約容器（型別 {type(values).__name__}；契約只接受 list）"
            ]
        entries = []
        for i, entry in enumerate(values):
            if not isinstance(entry, list) or len(entry) != 2:
                issues.append(
                    f"po_arrivals[{i}] ({side}): 每筆須為二欄 list [期數, 數量]（實際 {_safe_repr(entry)}）"
                )
                entries.append(None)
                continue
            t, q = entry
            ok_t = _is_finite_number(t) and _is_whole_number(t) and t >= 0
            ok_q = _is_finite_number(q)
            if not ok_t:
                issues.append(
                    f"po_arrivals[{i}][0] ({side}): 期數須為非負整數值（實際 {_safe_repr(t)}）"
                )
            if not ok_q:
                issues.append(
                    f"po_arrivals[{i}][1] ({side}): 數量須為有限數值（實際 {_safe_repr(q)}）"
                )
            entries.append([float(t), float(q)] if ok_t and ok_q else None)
        return entries, issues

    a_arr, a_arr_notes = _coerce_arrivals(baseline.po_arrivals, "baseline")
    b_arr, b_arr_notes = _coerce_arrivals(stockpyl.po_arrivals, "stockpyl")
    arrival_notes = a_arr_notes + b_arr_notes
    arrivals_match = False
    if a_arr is not None and b_arr is not None and not arrival_notes:
        if len(a_arr) == len(b_arr):
            arrivals_match = all(
                abs(x[0] - y[0]) <= NUMERIC_TOLERANCE and abs(x[1] - y[1]) <= NUMERIC_TOLERANCE
                for x, y in zip(a_arr, b_arr)
            )
            if not arrivals_match:
                arrival_notes.append(
                    f"po_arrivals: 二欄數值超過容差（baseline={_safe_repr(baseline.po_arrivals)} "
                    f"stockpyl={_safe_repr(stockpyl.po_arrivals)}）"
                )
        else:
            arrival_notes.append(
                f"po_arrivals: 筆數不同（baseline={len(a_arr)} stockpyl={len(b_arr)}）"
            )
    cmp["po_arrivals"] = {
        "baseline": baseline.po_arrivals,
        "stockpyl": stockpyl.po_arrivals,
        "match": arrivals_match,
    }
    cmp["po_quantity"] = _scalar("po_quantity", baseline.po_quantity, stockpyl.po_quantity)

    notes = []
    notes.extend(il_coerce_notes)
    notes.extend(arrival_notes)
    if cmp["inventory_levels"]["match"] is False:
        if a_il is not None and b_il is not None and (len(a_il) != len(b_il) or not a_il or not b_il):
            notes.append(
                f"inventory_levels: length mismatch or empty "
                f"(baseline={len(a_il)} stockpyl={len(b_il)}), cannot compare"
            )
        for i, x, y in non_finite_pairs:
            notes.append(
                f"inventory_levels[{i}]: non-finite or non-comparable values "
                f"(baseline={_safe_repr(x)} stockpyl={_safe_repr(y)}); treated as mismatch"
            )
        if max_abs_diff is not None:
            notes.append(
                f"inventory_levels: max_abs_diff={max_abs_diff} (> tol {NUMERIC_TOLERANCE})"
            )
    for f in _COMPARED_SCALARS:
        entry = cmp[f]
        if entry["match"] is False:
            if entry["abs_diff"] is None and entry["baseline"] is not None and entry["stockpyl"] is not None:
                notes.append(
                    f"{f}: non-finite or non-comparable values "
                    f"(baseline={_safe_repr(entry['baseline'])} stockpyl={_safe_repr(entry['stockpyl'])})"
                )
            elif entry["abs_diff"] is None:
                notes.append(
                    f"{f}: one-sided None "
                    f"(baseline={_safe_repr(entry['baseline'])} stockpyl={_safe_repr(entry['stockpyl'])})"
                )
            else:
                notes.append(
                    f"{f}: baseline={entry['baseline']} stockpyl={entry['stockpyl']} "
                    f"abs_diff={entry['abs_diff']}"
                )
    for f in ("po_id", "po_quantity"):
        if cmp[f]["match"] is False:
            notes.append(
                f"{f}: baseline={_safe_repr(cmp[f]['baseline'])} stockpyl={_safe_repr(cmp[f]['stockpyl'])}"
            )
    cmp["reason"] = (
        "; ".join(notes)
        if notes
        else f"engines agree within tolerance {NUMERIC_TOLERANCE} on all compared fields "
             "(deterministic fixed-demand fixture; single-PO semantics: one PO arrival at t=lead, "
             "no replenishment)"
    )
    return cmp


def run_scenario(inp: ScenarioInput, engine: str = "both", llm: LLMAdapter | None = None) -> ScenarioRun:
    """執行情境：validate fail-closed；引擎數值計算與 LLM 解釋分離。

    LLM 只在數值計算完成後被呼叫，且收到「深拷貝快照」（copy.deepcopy(run)）——
    adapter 對快照的任何原地修改（含巢狀 inventory_levels、inputs）都不會影響正式
    ScenarioRun；LLM 失敗只記錄於 failures、explanation=None（acceptance #4）。
    """
    run = ScenarioRun(inputs=inp, engine_version=SCENARIO_ENGINE_VERSION)

    # 共同 validate 層：設計上不拋例外；萬一仍拋（防禦性），fail-closed 記入 failures
    # 並回空結果，不得讓未捕捉例外逸出 run_scenario（第四修正回合）。
    try:
        v = inp.validate()
    except Exception as exc:
        run.failures.append(f"validation crashed (fail-closed): {type(exc).__name__}: {_safe_repr(exc)}")
        if engine in ("baseline", "both"):
            run.baseline = _empty_result(inp, "baseline")
        if engine in ("stockpyl", "both"):
            run.stockpyl = _empty_result(inp, "stockpyl")
        return run
    if not v.ok:
        missing = ", ".join(v.needs_input)
        run.failures.append(f"missing required input: {missing}")
        if engine in ("baseline", "both"):
            run.baseline = _empty_result(inp, "baseline", v.needs_input)
        if engine in ("stockpyl", "both"):
            run.stockpyl = _empty_result(inp, "stockpyl", v.needs_input)
        return run

    def _compute(name: str, fn) -> ScenarioResult | None:
        try:
            return fn(inp)
        except Exception as exc:  # 引擎失敗記錄，不丟棄其他引擎結果
            run.failures.append(f"{name}: {type(exc).__name__}: {exc}")
            return None

    if engine in ("baseline", "both"):
        run.baseline = _compute("baseline", deterministic_baseline)
    if engine in ("stockpyl", "both"):
        run.stockpyl = _compute("stockpyl", stockpyl_simulation)
    if engine not in ("baseline", "stockpyl", "both"):
        run.failures.append(f"unknown engine: {engine!r}")
        return run

    # LLM 解釋：深拷貝快照，adapter 改不到正式 run。
    if llm is not None:
        has_numbers = bool(run.baseline and run.baseline.fill_rate is not None) or bool(
            run.stockpyl and run.stockpyl.fill_rate is not None
        )
        if has_numbers:
            try:
                run.explanation = llm.explain(copy.deepcopy(run))
            except Exception as exc:
                run.failures.append(f"llm_explanation_failed: {type(exc).__name__}: {exc}")
                run.explanation = None
    return run


def load_scenario_input(path: str) -> ScenarioInput:
    """從 JSON fixture 載入 ScenarioInput。"""
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    demand = payload.pop("demand", None)
    if demand is not None:
        demand = DemandSpec(**demand)
    return ScenarioInput(demand=demand, **payload)


def _input_to_dict(inp: ScenarioInput) -> dict:
    """輸入 → 標準 JSON 可表示 dict；被拒絕值以 rejected 標記取代（不猜數值）。"""
    return _json_safe_input(inp)


def _rejected_marker(field: str, original_type: str, original_value: str, reason: str) -> dict:
    """被契約拒絕／標準 JSON 不可表示值的明確標記：欄位、原值類型、原值描述、拒絕原因。"""
    return {
        "rejected": True,
        "field": field,
        "original_type": original_type,
        "original_value": original_value,
        "reason": reason,
    }


def _json_safe_value(v, field: str):
    """遞迴轉成標準 JSON 可表示值；NaN/inf/超大 int/其他型別 → rejected 標記。

    不猜成數值、不用 allow_nan=True；正常值原樣通過（canonical 讀回位元組穩定）。
    """
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        try:
            str(v)  # 超大 int（如 10**10000）在 int_max_str_digits 下拋 ValueError
            return v
        except ValueError:
            return _rejected_marker(
                field,
                "int",
                f"<int: {v.bit_length()} bits>",
                "整數超出標準 JSON 安全表示上限（int_max_str_digits），未猜成數值",
            )
    if isinstance(v, float):
        if math.isfinite(v):
            return v
        original = "nan" if math.isnan(v) else ("inf" if v > 0 else "-inf")
        return _rejected_marker(field, "float", original, "非有限浮點數（NaN/Infinity）不合契約")
    if isinstance(v, DemandSpec):
        return {
            attr: _json_safe_value(getattr(v, attr, None), f"{field}.{attr}")
            for attr in ("type", "demand_list", "mean", "standard_deviation")
        }
    if isinstance(v, dict):
        out = {}
        for k, vv in v.items():
            key = k if isinstance(k, str) else _safe_repr(k)
            out[key] = _json_safe_value(vv, f"{field}.{key}")
        return out
    if isinstance(v, (list, tuple)):
        return [_json_safe_value(vv, f"{field}[{i}]") for i, vv in enumerate(v)]
    return _rejected_marker(
        field,
        type(v).__name__,
        _safe_repr(v),
        f"型別 {type(v).__name__} 非標準 JSON 可表示且不合契約",
    )


def _json_safe_input(inp: ScenarioInput) -> dict:
    """ScenarioInput → 標準 JSON dict（欄位順序與 asdict 一致）。"""
    fields = (
        "case_id", "scenario_id", "input_version", "seed", "horizon_days", "sku",
        "po_id", "po_quantity", "initial_stock", "lead_time_days", "holding_cost",
        "stockout_cost", "demand", "assumption_source", "description",
    )
    return {f: _json_safe_value(getattr(inp, f, None), f) for f in fields}


def _dict_to_input(d: dict) -> ScenarioInput:
    demand = d.pop("demand", None)
    if demand is not None:
        try:
            demand = DemandSpec(**demand)
        except TypeError:
            demand = None  # rejected 標記 dict 等無法重建形式：保留未知，不猜
    return ScenarioInput(demand=demand, **d)


def _safe_identifier(value, field: str) -> str:
    """識別碼／檔名元件驗證：只允許 [A-Za-z0-9_.-]{1,64}，拒絕絕對路徑、..、分隔符。"""
    if (
        not isinstance(value, str)
        or not _IDENTIFIER_RE.match(value)
        or value in (".", "..")
        or value.startswith("..")
    ):
        raise ValueError(
            f"{field}={value!r} 不是合法識別碼（只允許 [A-Za-z0-9_.-]、長度 1–64，"
            "不得含路徑分隔符、不得為 . 或 ..）"
        )
    return value


def persist_run(run: ScenarioRun, out_dir: str) -> str:
    """保存實際執行結果：版本、seed、完整輸入、結果、失敗項目（acceptance #6）。

    路徑安全：case_id / scenario_id / po_id 逐項驗證 + resolved containment，
    最終路徑必須位於 out_dir 內（GPT-6 verdict #6）。
    """
    from dataclasses import asdict

    if run.inputs is None:
        raise ValueError("ScenarioRun.inputs 為 None，無法保存")
    inp = run.inputs
    case_id = _safe_identifier(inp.case_id or "case", "case_id")
    scenario_id = _safe_identifier(inp.scenario_id or "scenario", "scenario_id")
    po_id = _safe_identifier(inp.po_id or "po", "po_id")
    if not isinstance(inp.seed, int):
        raise ValueError(f"seed={_safe_repr(inp.seed)} 非整數，拒絕組入檔名")
    try:
        str(inp.seed)  # 超大 int seed 無法安全組入檔名 → fail-closed 拒絕
    except ValueError:
        raise ValueError("seed 超出安全表示範圍，拒絕組入檔名") from None
    if inp.input_version is not None:
        if not isinstance(inp.input_version, int):
            raise ValueError(f"input_version={_safe_repr(inp.input_version)} 非整數，拒絕組入檔名")
        try:
            str(inp.input_version)
        except ValueError:
            raise ValueError("input_version 超出安全表示範圍，拒絕組入檔名") from None
    version = inp.input_version if inp.input_version is not None else "na"

    out = pathlib.Path(out_dir).resolve()
    name = f"{case_id}-{scenario_id}-{po_id}-seed{inp.seed}-v{version}.json"
    target = (out / name).resolve()
    if target.parent != out:
        raise ValueError(f"保存路徑 {target} 越出 out_dir={out}")

    # inputs/results 經 _json_safe_value：被契約拒絕的 NaN/inf/超大 int 以 rejected
    # 標記保存（欄位、原值類型、拒絕原因），allow_nan=False 產出標準 JSON。
    payload = {
        "schema": "scenario-run/1",
        "engine_version": SCENARIO_ENGINE_VERSION,
        "stockpyl_version": get_stockpyl_version(),
        "inputs": _json_safe_input(inp),
        "results": {
            "baseline": _json_safe_value(asdict(run.baseline), "results.baseline") if run.baseline is not None else None,
            "stockpyl": _json_safe_value(asdict(run.stockpyl), "results.stockpyl") if run.stockpyl is not None else None,
        },
        "explanation": run.explanation,
        "failures": list(run.failures),
    }
    out.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    return str(target)


def load_run(path: str) -> ScenarioRun:
    """載入 persist_run 的結果。"""
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    inp = _dict_to_input(dict(payload["inputs"]))

    def _result(d: dict | None) -> ScenarioResult | None:
        if d is None:
            return None
        return ScenarioResult(**d)

    return ScenarioRun(
        inputs=inp,
        baseline=_result(payload["results"].get("baseline")),
        stockpyl=_result(payload["results"].get("stockpyl")),
        explanation=payload.get("explanation"),
        failures=list(payload.get("failures") or []),
        engine_version=payload.get("engine_version", SCENARIO_ENGINE_VERSION),
        stockpyl_version=payload.get("stockpyl_version"),
    )
