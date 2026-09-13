"""
tests/test_scenario_engine.py
獨立情境契約 + Stockpyl adapter 的 TDD 測試。

範圍（對應 vault `19-學術專題開源整合候選重查.md:95-146、373-377、400-404`
與 `18-2026產品功能與技術架構重估.md:150-156、301-306`）：
  - 單一 SKU、單一 PO 的正常 / 中斷 / 替代情境 fixture。
  - deterministic baseline（手算可驗算）對照 Stockpyl 模擬。
  - 六項 acceptance tests 逐條對應的測試函式。

事前固定的合成案例（所有測試共用，非企業資料；2026-09-12 修正回合改為單一 PO 語意）：
  - 需求 D=10/期（固定 20 期）、單張 PO Q=200、初始庫存 I0=15、h=1、p=50。
  - 單一 PO 語意（GPT-6 verdict #3）：t=lead 一次到貨 Q，之後無任何補貨
    （非 base-stock、非無限供應）。
  - 每期事件順序（與 Stockpyl state_vars 實測對齊）：先到貨（先補 backorder、
    餘量入現貨）→ 再遇需求 → 期末 IL_t。
  - 手算基準（IL 為期末值）：
      L=1（正常） : IL=[5]+[195,185,...,15]、無缺貨、fill=200/200=1.0、成本=2000。
      L=2（替代） : IL=[5,-5]+[185,175,...,15]、首次缺貨 t=1、fill=195/200=0.975、成本=2055。
      L=3（中斷） : IL=[5,-5,-15]+[175,165,...,15]、首次缺貨 t=1、fill=185/200=0.925、成本=2620。
  - 容差（事前固定）：NUMERIC_TOLERANCE = 1e-9。
"""

from __future__ import annotations

import copy
import json
import math
import pathlib

import pytest

from backend.scenario_contract import (
    NUMERIC_TOLERANCE,
    SCENARIO_ENGINE_VERSION,
    DemandSpec,
    ScenarioInput,
    ScenarioResult,
)
from backend.scenario_engine import (
    FailingLLMAdapter,
    compare_results,
    deterministic_baseline,
    get_stockpyl_version,
    load_run,
    load_scenario_input,
    persist_run,
    run_scenario,
    stockpyl_simulation,
)

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "scenarios"

# 事前固定數值容差（acceptance #2）。所有 baseline↔stockpyl 數值比對統一用此值。
TOL = NUMERIC_TOLERANCE


def make_input(**overrides) -> ScenarioInput:
    """標準合成 fixture 工廠：單一 SKU、單一 PO（I0=15、Q=200、D=10×20、h=1、p=50）。"""
    base = dict(
        case_id="CASE-SKU-001",
        scenario_id="normal",
        input_version=1,
        seed=0,
        horizon_days=20,
        sku="SKU-001",
        po_id="PO-001",
        po_quantity=200.0,
        initial_stock=15.0,
        lead_time_days=1,
        holding_cost=1.0,
        stockout_cost=50.0,
        demand=DemandSpec(type="D", demand_list=[10.0] * 20),
        assumption_source="synthetic-fixture/stockpyl-spike-v1 (hand-computable fixed demand; not enterprise data)",
    )
    base.update(overrides)
    return ScenarioInput(**base)


# ──────────────────────────────────────────────────────────────────────────
# 修正回合新增：Comparator 正確性（GPT-6 verdict 已重現之 P1）
#  - 長度不同／單邊空 → match=False，不得 max_abs_diff=0 且宣稱一致
#  - 單邊 None、stockout_periods、demand_total_realized mismatch → reason 必須逐項列出
# ──────────────────────────────────────────────────────────────────────────

def test_comparator_length_mismatch_is_not_a_match():
    a = ScenarioResult(engine="baseline", inventory_levels=[1.0])
    b = ScenarioResult(engine="stockpyl", inventory_levels=[])
    cmp = compare_results(a, b)
    assert cmp["inventory_levels"]["match"] is False
    assert cmp["inventory_levels"]["max_abs_diff"] is None  # 不能是 0.0
    assert "inventory_levels" in cmp["reason"]


def test_comparator_one_sided_empty_is_not_a_match():
    a = ScenarioResult(engine="baseline", inventory_levels=[])
    b = ScenarioResult(engine="stockpyl", inventory_levels=[])
    cmp = compare_results(a, b)
    assert cmp["inventory_levels"]["match"] is False  # 無資料可比較＝不得宣稱一致
    assert "inventory_levels" in cmp["reason"]


def test_comparator_one_sided_none_scalar_is_listed_in_reason():
    a = ScenarioResult(
        engine="baseline", inventory_levels=[1.0], fill_rate=0.5, holding_cost=1.0,
        stockout_cost=2.0, total_cost=3.0, mean_cost_per_day=0.1, stockout_periods=0,
        demand_total_realized=10.0, first_stockout_period=None,
    )
    b = ScenarioResult(
        engine="stockpyl", inventory_levels=[1.0], fill_rate=0.5, holding_cost=1.0,
        stockout_cost=2.0, total_cost=None, mean_cost_per_day=0.1, stockout_periods=0,
        demand_total_realized=10.0, first_stockout_period=None,
    )
    cmp = compare_results(a, b)
    assert cmp["total_cost"]["match"] is False
    assert cmp["total_cost"]["abs_diff"] is None
    assert "total_cost" in cmp["reason"]


def test_comparator_stockout_periods_mismatch_is_listed_in_reason():
    a = ScenarioResult(engine="baseline", stockout_periods=1, inventory_levels=[1.0])
    b = ScenarioResult(engine="stockpyl", stockout_periods=2, inventory_levels=[1.0])
    cmp = compare_results(a, b)
    assert cmp["stockout_periods"]["match"] is False
    assert cmp["stockout_periods"]["abs_diff"] == 1
    assert "stockout_periods" in cmp["reason"]


def test_comparator_demand_total_realized_mismatch_is_listed_in_reason():
    a = ScenarioResult(engine="baseline", demand_total_realized=10.0, inventory_levels=[1.0])
    b = ScenarioResult(engine="stockpyl", demand_total_realized=20.0, inventory_levels=[1.0])
    cmp = compare_results(a, b)
    assert cmp["demand_total_realized"]["match"] is False
    assert "demand_total_realized" in cmp["reason"]


def test_comparator_reason_lists_every_mismatched_field():
    # 同時多欄位錯（含單邊 None）：reason 必須逐項列出每個 mismatch 欄位名。
    a = ScenarioResult(
        engine="baseline", inventory_levels=[1.0, 2.0], fill_rate=0.5,
        total_cost=None, stockout_periods=1, demand_total_realized=10.0,
    )
    b = ScenarioResult(
        engine="stockpyl", inventory_levels=[3.0], fill_rate=0.9,
        total_cost=1.0, stockout_periods=2, demand_total_realized=20.0,
    )
    cmp = compare_results(a, b)
    for f in ("inventory_levels", "fill_rate", "total_cost", "stockout_periods", "demand_total_realized"):
        assert cmp[f]["match"] is False, f
    for f in ("inventory_levels", "fill_rate", "total_cost", "stockout_periods", "demand_total_realized"):
        assert f in cmp["reason"], f"reason 未列出 {f}: {cmp['reason']}"


# ──────────────────────────────────────────────────────────────────────────
# 修正回合新增：seed 測試弱點（GPT-6 verdict #2）
#  - canonical bytes 內含 seed，不能拿來證明 seed 影響數值
#  - 直接比較數值軌跡／需求實現／成本欄位
# ──────────────────────────────────────────────────────────────────────────

def test_seed_changes_actual_numeric_trajectory():
    """不同 seed → 至少一個實際數值欄位改變（非 canonical bytes 差異）。"""
    def run(seed):
        return stockpyl_simulation(
            make_input(
                seed=seed,
                scenario_id="stochastic",
                demand=DemandSpec(type="N", mean=10.0, standard_deviation=3.0),
            )
        )

    s0 = run(0)
    s1 = run(12345)
    changed = (
        s0.demand_total_realized != s1.demand_total_realized
        or s0.inventory_levels != s1.inventory_levels
        or s0.total_cost != s1.total_cost
    )
    assert changed, (
        f"seed 0 vs 12345 的數值完全相同：demand_total_realized "
        f"{s0.demand_total_realized} vs {s1.demand_total_realized}"
    )
    # 同 seed 重跑仍位元組一致（既有 acceptance #1）
    assert run(0).canonical_bytes() == run(0).canonical_bytes()


# ──────────────────────────────────────────────────────────────────────────
# 修正回合新增：LLM 硬邊界（GPT-6 verdict #4）
#  - llm.explain() 不得拿到與正式 ScenarioRun 共用的可變參照
#  - adapter 直接改 baseline/stockpyl 數值、巢狀 inventory list、inputs，甚至改完拋錯
#  - 正式 run 必須完全不變
# ──────────────────────────────────────────────────────────────────────────

class _MutatingLLM:
    """惡意 adapter：直接修改傳入物件的數值欄位與巢狀 list。"""

    def explain(self, run):
        run.baseline.fill_rate = 999.0
        run.baseline.total_cost = -1.0
        run.baseline.inventory_levels[0] = 999.0
        run.inputs.lead_time_days = 99
        return "mutated the run in place"


def test_llm_receives_snapshot_cannot_mutate_run():
    inp = make_input(lead_time_days=3)
    before = deterministic_baseline(inp)
    run = run_scenario(inp, engine="both", llm=_MutatingLLM())
    assert run.baseline.fill_rate == before.fill_rate
    assert run.baseline.total_cost == before.total_cost
    assert run.baseline.inventory_levels[0] == before.inventory_levels[0]
    assert run.inputs.lead_time_days == 3
    assert run.stockpyl is not None
    assert run.stockpyl.fill_rate is not None  # 數值仍正常產出


class _MutateThenRaiseLLM:
    """惡意 adapter：先改數值，再丟例外。"""

    def explain(self, run):
        run.baseline.total_cost = -1.0
        run.inputs.lead_time_days = 99
        raise RuntimeError("boom after mutation")


def test_llm_mutation_then_exception_does_not_affect_results():
    inp = make_input(lead_time_days=3)
    run = run_scenario(inp, engine="both", llm=_MutateThenRaiseLLM())
    assert run.baseline.total_cost == pytest.approx(2620.0, abs=TOL)  # 單一 PO：L=3,p=50 手算 2620
    assert run.inputs.lead_time_days == 3
    assert run.explanation is None
    assert any("llm_explanation_failed" in f for f in run.failures)


# ──────────────────────────────────────────────────────────────────────────
# 修正回合新增：輸入 fail-closed 與假設範圍（GPT-6 verdict #5）
# ──────────────────────────────────────────────────────────────────────────

def test_fractional_lead_time_fails_closed_in_common_validation():
    """lead_time_days=1.5 必須在共同 validate 層 fail-closed，不得 round() 成 2。"""
    inp = make_input(lead_time_days=1.5)
    v = inp.validate()
    assert not v.ok
    assert "lead_time_days" in v.needs_input
    run = run_scenario(inp, engine="both")
    assert "lead_time_days" in run.baseline.needs_input
    assert "lead_time_days" in run.stockpyl.needs_input
    assert run.baseline.inventory_levels == []
    assert run.baseline.total_cost is None  # 不得用 lead=2 算出一組成本


def test_synthetic_fixture_inputs_all_marked_as_assumptions():
    """initial_stock、PO quantity／arrival、demand、交期、成本全部要標 assumption+source。"""
    inp = make_input(lead_time_days=3)
    res = deterministic_baseline(inp)
    marked = {a["field"] for a in res.assumptions}
    assert {"initial_stock", "po_quantity", "lead_time_days", "demand",
            "holding_cost", "stockout_cost"} <= marked
    for a in res.assumptions:
        assert a["assumption"] is True
        assert a["source"]


# ──────────────────────────────────────────────────────────────────────────
# 修正回合新增：persist 路徑穿越（GPT-6 verdict #6）
# ──────────────────────────────────────────────────────────────────────────

def test_persist_rejects_path_traversal_identifiers(tmp_path):
    """case_id/scenario_id 不得造成絕對路徑、.. 或分隔符越過 out_dir。"""
    for field in ("case_id", "scenario_id"):
        for bad in ("/tmp/outside", "../evil", "a/b", "..", "...", ".", "C:\\evil", "x" * 100):
            inp = make_input(lead_time_days=3, **{field: bad})
            run = run_scenario(inp, engine="baseline")
            with pytest.raises(ValueError):
                persist_run(run, str(tmp_path))
    assert list(tmp_path.iterdir()) == []  # 全部被拒絕，out_dir 內不該寫出任何檔案


def test_persist_valid_ids_stay_inside_out_dir(tmp_path):
    inp = make_input(lead_time_days=3, case_id="CASE-A", scenario_id="normal", po_id="PO-001")
    run = run_scenario(inp, engine="baseline")
    path = pathlib.Path(persist_run(run, str(tmp_path)))
    assert path.exists()
    assert path.resolve().parent == pathlib.Path(str(tmp_path)).resolve()


# ──────────────────────────────────────────────────────────────────────────
# 修正回合新增：單一 SKU、單一 PO 契約語意（GPT-6 verdict #3）
#  - 契約須有明示 PO quantity 與 arrival timing/lead
#  - horizon 內只允許該 PO 一次到貨（不是無限供應 + 每期補貨）
#  - po_id 參與保存／輸出語意
#  - 手算預期（I0=15、Q=200、D=10×20、h=1、p=50、到貨於 t=lead）：
#      L=1: IL=[5,195..15]、fill=1.0、hold=2000、so=0、total=2000
#      L=2: IL=[5,-5,185..15]、fill=0.975、hold=1805、so=250、total=2055
#      L=3: IL=[5,-5,-15,175..15]、fill=0.925、hold=1620、so=1000、total=2620
# ──────────────────────────────────────────────────────────────────────────

def test_single_po_contract_requires_explicit_po_fields():
    v = make_input(po_quantity=None).validate()
    assert "po_quantity" in v.needs_input
    assert not v.ok
    v2 = make_input(po_id=None).validate()
    assert "po_id" in v2.needs_input
    v3 = make_input(po_id="").validate()
    assert "po_id" in v3.needs_input
    v4 = make_input(po_quantity=0.0).validate()
    assert "po_quantity" in v4.needs_input
    # 到貨 timing 必須落在 horizon 內
    v5 = make_input(lead_time_days=20).validate()
    assert "lead_time_days" in v5.needs_input


def test_single_po_baseline_hand_calc_normal():
    res = deterministic_baseline(make_input(lead_time_days=1))
    assert res.inventory_levels == [5.0] + [195.0 - 10.0 * k for k in range(19)]
    assert res.first_stockout_period is None
    assert res.stockout_periods == 0
    assert res.fill_rate == pytest.approx(1.0, abs=TOL)
    assert res.holding_cost == pytest.approx(2000.0, abs=TOL)
    assert res.stockout_cost == pytest.approx(0.0, abs=TOL)
    assert res.total_cost == pytest.approx(2000.0, abs=TOL)


def test_single_po_baseline_hand_calc_substitute():
    res = deterministic_baseline(make_input(lead_time_days=2))
    assert res.inventory_levels == [5.0, -5.0] + [185.0 - 10.0 * k for k in range(18)]
    assert res.first_stockout_period == 1
    assert res.fill_rate == pytest.approx(195.0 / 200.0, abs=TOL)
    assert res.holding_cost == pytest.approx(1805.0, abs=TOL)
    assert res.stockout_cost == pytest.approx(250.0, abs=TOL)
    assert res.total_cost == pytest.approx(2055.0, abs=TOL)


def test_single_po_baseline_hand_calc_disruption():
    res = deterministic_baseline(make_input(lead_time_days=3))
    assert res.inventory_levels == [5.0, -5.0, -15.0] + [175.0 - 10.0 * k for k in range(17)]
    assert res.first_stockout_period == 1
    assert res.fill_rate == pytest.approx(185.0 / 200.0, abs=TOL)
    assert res.holding_cost == pytest.approx(1620.0, abs=TOL)
    assert res.stockout_cost == pytest.approx(1000.0, abs=TOL)
    assert res.total_cost == pytest.approx(2620.0, abs=TOL)


def test_single_po_only_one_arrival_baseline():
    assert deterministic_baseline(make_input(lead_time_days=1)).po_arrivals == [[1, 200.0]]
    assert deterministic_baseline(make_input(lead_time_days=2)).po_arrivals == [[2, 200.0]]
    assert deterministic_baseline(make_input(lead_time_days=3)).po_arrivals == [[3, 200.0]]


def test_single_po_stockpyl_matches_baseline_and_one_arrival():
    for lead in (1, 2, 3):
        inp = make_input(lead_time_days=lead)
        base = deterministic_baseline(inp)
        sp = stockpyl_simulation(inp)
        assert sp.po_arrivals == [[lead, 200.0]], f"lead={lead}: po_arrivals={sp.po_arrivals}"
        assert sp.inventory_levels == base.inventory_levels
        assert sp.first_stockout_period == base.first_stockout_period
        assert sp.fill_rate == pytest.approx(base.fill_rate, abs=TOL)
        assert sp.holding_cost == pytest.approx(base.holding_cost, abs=TOL)
        assert sp.stockout_cost == pytest.approx(base.stockout_cost, abs=TOL)
        assert sp.total_cost == pytest.approx(base.total_cost, abs=TOL)
        assert sp.inventory_levels[-1] == pytest.approx(15.0, abs=TOL)  # I0+Q−總需求


def test_po_id_and_quantity_participate_in_result_and_canonical():
    a = deterministic_baseline(make_input(po_id="PO-001"))
    b = deterministic_baseline(make_input(po_id="PO-002"))
    assert a.po_id == "PO-001"
    assert b.po_id == "PO-002"
    assert a.po_quantity == 200.0
    payload_a = json.loads(a.canonical_bytes())
    assert payload_a["po_id"] == "PO-001"
    assert payload_a["po_quantity"] == 200.0
    assert payload_a["po_arrivals"] == [[1, 200.0]]
    assert a.canonical_bytes() != b.canonical_bytes()


# ──────────────────────────────────────────────────────────────────────────
# Acceptance 5：缺少必要需求或交期 → needs_input；成本缺失不得回傳推測成本；
#               明示合成假設標示 assumption 與來源。
# ──────────────────────────────────────────────────────────────────────────

def test_missing_demand_returns_needs_input():
    inp = make_input(demand=DemandSpec(type="D", demand_list=None))
    run = run_scenario(inp, engine="baseline")
    assert "demand" in run.baseline.needs_input
    assert run.baseline.inventory_levels == []
    assert run.baseline.fill_rate is None


def test_missing_lead_time_returns_needs_input():
    inp = make_input(lead_time_days=None)
    run = run_scenario(inp, engine="baseline")
    assert "lead_time_days" in run.baseline.needs_input


def test_missing_normal_distribution_params_returns_needs_input():
    inp = make_input(demand=DemandSpec(type="N", mean=10.0, standard_deviation=None))
    run = run_scenario(inp, engine="baseline")
    assert "demand" in run.baseline.needs_input


def test_missing_cost_is_not_guessed():
    inp = make_input(lead_time_days=3, holding_cost=None, stockout_cost=None)
    res = deterministic_baseline(inp)
    assert res.costs_provided is False
    assert res.holding_cost is None
    assert res.stockout_cost is None
    assert res.total_cost is None
    assert res.mean_cost_per_day is None
    # 庫存 / 缺貨 / 填充率仍可計算（不需成本）
    assert res.inventory_levels
    assert res.fill_rate is not None
    assert res.first_stockout_period == 1


def test_provided_synthetic_assumptions_are_marked_with_source():
    inp = make_input(
        holding_cost=1.0,
        stockout_cost=50.0,
        assumption_source="synthetic-fixture/stockpyl-spike-v1",
    )
    res = deterministic_baseline(inp)
    assert res.costs_provided is True
    marked = {a["field"] for a in res.assumptions}
    assert {"holding_cost", "stockout_cost", "demand", "lead_time_days",
            "initial_stock", "po_quantity"} <= marked
    for a in res.assumptions:
        assert a["source"] == "synthetic-fixture/stockpyl-spike-v1"
        assert a["assumption"] is True


# ──────────────────────────────────────────────────────────────────────────
# Acceptance 2（Stockpyl 對照）：固定容差 + 差異原因紀錄。
# ──────────────────────────────────────────────────────────────────────────

def test_stockpyl_matches_baseline_within_tolerance():
    for lead in (1, 2, 3):
        inp = make_input(lead_time_days=lead)
        base = deterministic_baseline(inp)
        sp = stockpyl_simulation(inp)
        cmp = compare_results(base, sp)
        assert cmp["inventory_levels"]["max_abs_diff"] <= TOL
        for f in ("fill_rate", "holding_cost", "stockout_cost", "total_cost",
                  "mean_cost_per_day", "stockout_periods", "demand_total_realized",
                  "first_stockout_period"):
            assert cmp[f]["match"] is True, f"lead={lead} 欄位 {f} 不一致: {cmp[f]}"
        assert cmp["po_id"]["match"] is True
        assert cmp["po_arrivals"]["match"] is True
        # 差異原因必須被記錄（不能是空字串）
        assert cmp["reason"]


def test_stockpyl_version_reported():
    assert get_stockpyl_version() == "1.0.2"


# ──────────────────────────────────────────────────────────────────────────
# Acceptance 3：正常 / 中斷 / 替代 fixture 的預期關係（先寫入測試，
# 只對受控 fixture 驗證，不宣稱任意隨機模型必然單調）。
# ──────────────────────────────────────────────────────────────────────────

def test_fixture_files_exist_and_load():
    for name in ("normal_single_sku.json", "substitute_single_sku.json", "disruption_single_sku.json"):
        path = FIXTURES_DIR / name
        assert path.exists(), f"fixture 缺少 {name}"
        inp = load_scenario_input(str(path))
        assert inp.validate().ok
        assert inp.po_quantity == 200.0
        assert inp.po_id
        res = deterministic_baseline(inp)
        assert res.inventory_levels
        assert res.fill_rate is not None


def test_fixture_expected_relationships():
    normal = stockpyl_simulation(load_scenario_input(str(FIXTURES_DIR / "normal_single_sku.json")))
    substitute = stockpyl_simulation(load_scenario_input(str(FIXTURES_DIR / "substitute_single_sku.json")))
    disruption = stockpyl_simulation(load_scenario_input(str(FIXTURES_DIR / "disruption_single_sku.json")))

    # 手算基準（單一 PO、到貨 t=lead、p=50）
    assert normal.fill_rate == pytest.approx(1.0, abs=TOL)
    assert normal.first_stockout_period is None
    assert normal.total_cost == pytest.approx(2000.0, abs=TOL)

    assert substitute.fill_rate == pytest.approx(195.0 / 200.0, abs=TOL)
    assert substitute.first_stockout_period == 1
    assert substitute.total_cost == pytest.approx(2055.0, abs=TOL)

    assert disruption.fill_rate == pytest.approx(185.0 / 200.0, abs=TOL)
    assert disruption.first_stockout_period == 1
    assert disruption.total_cost == pytest.approx(2620.0, abs=TOL)

    # 預期關係（只對受控 fixture 驗證）
    assert disruption.fill_rate < normal.fill_rate
    assert substitute.fill_rate >= disruption.fill_rate

    def _order(so):  # None（永不缺貨）視為 +inf
        return math.inf if so is None else so

    assert _order(substitute.first_stockout_period) >= _order(disruption.first_stockout_period)
    assert substitute.fill_rate <= normal.fill_rate

    # 成本排序（嚴格遞增）：2000 < 2055 < 2620
    assert normal.total_cost < substitute.total_cost < disruption.total_cost

    # 單一 PO：三個 fixture 都只有一次到貨
    for res in (normal, substitute, disruption):
        assert len(res.po_arrivals) == 1


# ──────────────────────────────────────────────────────────────────────────
# Acceptance 1：同一輸入、版本、seed 重跑 → canonical bytes 一致（排除時間）。
# ──────────────────────────────────────────────────────────────────────────

def test_deterministic_fixture_rerun_is_byte_stable():
    inp = make_input(lead_time_days=3)
    a = run_scenario(inp, engine="both")
    b = run_scenario(inp, engine="both")
    assert a.baseline.canonical_bytes() == b.baseline.canonical_bytes()
    assert a.stockpyl.canonical_bytes() == b.stockpyl.canonical_bytes()


def test_seeded_stochastic_rerun_is_byte_stable():
    inp = make_input(
        scenario_id="stochastic",
        demand=DemandSpec(type="N", mean=10.0, standard_deviation=3.0),
    )
    a = stockpyl_simulation(inp)
    b = stockpyl_simulation(inp)
    assert a.canonical_bytes() == b.canonical_bytes()


def test_canonical_bytes_exclude_execution_metadata():
    inp = make_input(lead_time_days=3)
    r1 = deterministic_baseline(inp)
    r1.executed_at = "2026-01-01T00:00:00"
    r1.execution_time_seconds = 1.0
    r2 = deterministic_baseline(inp)
    r2.executed_at = "2026-12-31T23:59:59"
    r2.execution_time_seconds = 999.0
    assert r1.canonical_bytes() == r2.canonical_bytes()


# ──────────────────────────────────────────────────────────────────────────
# Acceptance 4：LLM adapter 呼叫即失敗時，數值計算仍完成；LLM 不得覆寫數值。
# ──────────────────────────────────────────────────────────────────────────

def test_failing_llm_does_not_block_numeric_results():
    inp = make_input(lead_time_days=3)
    run = run_scenario(inp, engine="both", llm=FailingLLMAdapter())
    assert run.baseline.fill_rate is not None
    assert run.baseline.total_cost is not None
    assert run.stockpyl.fill_rate is not None
    assert run.stockpyl.total_cost is not None
    assert run.explanation is None  # LLM 失敗不阻斷，也不塞假解釋


class _NumberOverwritingLLM:
    """惡意 adapter：試圖回傳一組與引擎不同的數字。"""

    def explain(self, run):
        return "fill_rate=999.0 total_cost=-1.0 這是 LLM 自行編造的數字"


def test_llm_cannot_overwrite_numeric_fields():
    inp = make_input(lead_time_days=3)
    before = stockpyl_simulation(inp)
    run = run_scenario(inp, engine="stockpyl", llm=_NumberOverwritingLLM())
    assert run.stockpyl.fill_rate == before.fill_rate
    assert run.stockpyl.total_cost == before.total_cost
    assert "999.0" not in str(run.stockpyl.canonical_bytes())
    assert run.explanation == "fill_rate=999.0 total_cost=-1.0 這是 LLM 自行編造的數字"


# ──────────────────────────────────────────────────────────────────────────
# Acceptance 6：保存實際執行結果、版本、seed 與失敗項目。
# ──────────────────────────────────────────────────────────────────────────

def test_persist_and_reload_run(tmp_path):
    inp = make_input(lead_time_days=3)
    run = run_scenario(inp, engine="both")
    path = persist_run(run, str(tmp_path))
    assert pathlib.Path(path).exists()
    assert "PO-001" in pathlib.Path(path).name  # po_id 參與保存檔名語意
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    assert payload["engine_version"] == SCENARIO_ENGINE_VERSION
    assert payload["stockpyl_version"] == get_stockpyl_version()
    assert payload["inputs"]["seed"] == 0
    assert payload["inputs"]["input_version"] == 1
    assert payload["inputs"]["po_quantity"] == 200.0
    assert payload["failures"] == []
    assert payload["results"]["stockpyl"]["total_cost"] is not None

    loaded = load_run(path)
    assert loaded.inputs.seed == 0
    assert loaded.stockpyl.total_cost is not None
    assert loaded.stockpyl.po_id == "PO-001"
    assert loaded.stockpyl.po_arrivals == [[3, 200.0]]
    assert loaded.stockpyl.canonical_bytes() == run.stockpyl.canonical_bytes()


def test_persist_records_failures(tmp_path):
    inp = make_input(demand=DemandSpec(type="D", demand_list=None))
    run = run_scenario(inp, engine="both")
    assert run.failures
    path = persist_run(run, str(tmp_path))
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    assert payload["failures"]
    assert any("demand" in f for f in payload["failures"])


# ──────────────────────────────────────────────────────────────────────────
# 第二修正回合新增（GPT-6 最終 verdict）：
#   §1 comparator 對 NaN/Infinity/不可比較值 fail closed
#   §2 共同 validate 層型別/有限性/整數語意/值域攔截 + demand_list 逐項檢查
#   §3 Stockpyl adapter 殘留訂單簿記 + 1e12 冒充無限造成的到貨可用時間錯位
# ──────────────────────────────────────────────────────────────────────────

# ---- §1 comparator：非有限/不可比較值 → match=False、max_abs_diff=None、reason 明列欄位與索引 ----

@pytest.mark.parametrize("idx", [0, 1, 2])
def test_comparator_nan_at_index_fails_closed(idx):
    """NaN 出現在任一索引（首、中、末）→ match=False、max_abs_diff=None、reason 列索引。

    max() 不傳播後續 NaN：NaN 在 idx>0 時舊實作 max_abs_diff=0.0 且 match=True（P1）。
    """
    a = ScenarioResult(engine="baseline", inventory_levels=[1.0, 2.0, 3.0])
    b_il = [1.0, 2.0, 3.0]
    b_il[idx] = math.nan
    b = ScenarioResult(engine="stockpyl", inventory_levels=b_il)
    cmp = compare_results(a, b)
    assert cmp["inventory_levels"]["match"] is False
    assert cmp["inventory_levels"]["max_abs_diff"] is None
    assert "inventory_levels" in cmp["reason"]
    assert str(idx) in cmp["reason"]


def test_comparator_infinity_single_sided_fails_closed():
    """單邊 Infinity → match=False、max_abs_diff=None、reason 列欄位與索引。"""
    a = ScenarioResult(engine="baseline", inventory_levels=[1.0, 2.0])
    b = ScenarioResult(engine="stockpyl", inventory_levels=[math.inf, 2.0])
    cmp = compare_results(a, b)
    assert cmp["inventory_levels"]["match"] is False
    assert cmp["inventory_levels"]["max_abs_diff"] is None
    assert "inventory_levels" in cmp["reason"]
    assert "0" in cmp["reason"]


def test_comparator_infinity_double_sided_fails_closed():
    """雙邊 Infinity（inf-inf=nan）→ match=False、max_abs_diff=None。"""
    a = ScenarioResult(engine="baseline", inventory_levels=[math.inf, 2.0])
    b = ScenarioResult(engine="stockpyl", inventory_levels=[math.inf, 2.0])
    cmp = compare_results(a, b)
    assert cmp["inventory_levels"]["match"] is False
    assert cmp["inventory_levels"]["max_abs_diff"] is None
    assert "inventory_levels" in cmp["reason"]


def test_comparator_non_comparable_value_fails_closed_without_raise():
    """不可比較值（字串）→ 回傳結構化 mismatch，不得拋 TypeError。"""
    a = ScenarioResult(engine="baseline", inventory_levels=[1.0, 2.0])
    b = ScenarioResult(engine="stockpyl", inventory_levels=["x", 2.0])
    cmp = compare_results(a, b)  # 舊實作在此拋 TypeError
    assert cmp["inventory_levels"]["match"] is False
    assert cmp["inventory_levels"]["max_abs_diff"] is None
    assert "0" in cmp["reason"]


def test_comparator_scalar_nan_fails_closed_and_listed():
    """scalar 雙邊 NaN → match=False、abs_diff=None、reason 列出欄位名。"""
    a = ScenarioResult(engine="baseline", inventory_levels=[1.0], fill_rate=math.nan)
    b = ScenarioResult(engine="stockpyl", inventory_levels=[1.0], fill_rate=math.nan)
    cmp = compare_results(a, b)
    assert cmp["fill_rate"]["match"] is False
    assert cmp["fill_rate"]["abs_diff"] is None
    assert "fill_rate" in cmp["reason"]


def test_comparator_scalar_infinity_fails_closed_and_listed():
    """scalar 單邊 Infinity → match=False、abs_diff=None、reason 列出欄位名。"""
    a = ScenarioResult(engine="baseline", inventory_levels=[1.0], total_cost=math.inf)
    b = ScenarioResult(engine="stockpyl", inventory_levels=[1.0], total_cost=100.0)
    cmp = compare_results(a, b)
    assert cmp["total_cost"]["match"] is False
    assert cmp["total_cost"]["abs_diff"] is None
    assert "total_cost" in cmp["reason"]


# ---- §2 共同 validate 層：型別、有限性、整數語意、值域、demand_list 逐項 ----

def test_validation_demand_list_none_element_needs_input():
    """demand_list 逐期缺一項（None）仍是缺必要需求 → needs_input，不得進引擎。"""
    dl = [10.0] * 20
    dl[1] = None
    inp = make_input(demand=DemandSpec(type="D", demand_list=dl))
    v = inp.validate()
    assert not v.ok
    assert "demand" in v.needs_input
    assert any("demand_list[1]" in i for i in v.issues)
    run = run_scenario(inp, engine="both")
    assert run.baseline is not None and "demand" in run.baseline.needs_input
    assert run.stockpyl is not None and "demand" in run.stockpyl.needs_input
    assert run.baseline.inventory_levels == []
    assert run.stockpyl.inventory_levels == []
    assert any("missing required input" in f for f in run.failures)


def test_validation_horizon_float_rejected():
    """horizon_days=20.0（float）→ 共同 validate 明確拒絕，不得進引擎（range(20.0) 會崩）。"""
    inp = make_input(horizon_days=20.0)
    v = inp.validate()
    assert not v.ok
    assert "horizon_days" in v.needs_input
    run = run_scenario(inp, engine="both")
    assert run.baseline is not None and "horizon_days" in run.baseline.needs_input
    assert run.stockpyl is not None and "horizon_days" in run.stockpyl.needs_input
    assert run.baseline.inventory_levels == []
    assert any("missing required input" in f for f in run.failures)


def test_validation_lead_time_string_returns_needs_input_not_typeerror():
    """lead_time_days=\"1\" → needs_input，validate 不得拋未捕捉 TypeError。"""
    inp = make_input(lead_time_days="1")
    v = inp.validate()  # 舊實作在此拋 TypeError
    assert not v.ok
    assert "lead_time_days" in v.needs_input
    run = run_scenario(inp, engine="both")
    assert run.baseline is not None and "lead_time_days" in run.baseline.needs_input
    assert run.stockpyl is not None and "lead_time_days" in run.stockpyl.needs_input


def test_validation_po_quantity_string_returns_needs_input_not_typeerror():
    """po_quantity=\"200\" → needs_input，validate 不得拋未捕捉 TypeError。"""
    inp = make_input(po_quantity="200")
    v = inp.validate()  # 舊實作在此拋 TypeError
    assert not v.ok
    assert "po_quantity" in v.needs_input


@pytest.mark.parametrize("field,val", [
    ("initial_stock", math.nan),
    ("initial_stock", math.inf),
    ("po_quantity", math.nan),
    ("po_quantity", math.inf),
])
def test_validation_nan_infinity_rejected(field, val):
    """NaN/Infinity 輸入 → needs_input，不得被接受而產出非有限結果。"""
    inp = make_input(**{field: val})
    v = inp.validate()
    assert not v.ok, f"{field}={val} 不應通過驗證"
    assert field in v.needs_input


def test_validation_lead_time_infinity_rejected_not_overflow():
    """lead_time_days=inf → needs_input；validate 不得對非有限值拋 OverflowError。

    （舊實作對 inf 已回 needs_input；本測試為回歸 pin，鎖定行為不得退化，
    且 _is_whole_number 不得在 inf 上呼叫 is_integer()。）
    """
    inp = make_input(lead_time_days=math.inf)
    v = inp.validate()
    assert not v.ok
    assert "lead_time_days" in v.needs_input


def test_validation_negative_demand_rejected():
    """負需求 → needs_input（舊實作接受，Stockpyl fill_rate 可 >1）。"""
    dl = [10.0] * 20
    dl[3] = -5.0
    inp = make_input(demand=DemandSpec(type="D", demand_list=dl))
    v = inp.validate()
    assert not v.ok
    assert "demand" in v.needs_input
    assert any("demand_list[3]" in i for i in v.issues)


def test_validation_bad_inputs_fail_closed_via_both_engines():
    """不合約輸入跑兩引擎 → 全部回 needs_input 空結果，無任何未捕捉例外。"""
    bad_dl = [10.0] * 20
    bad_dl[1] = None
    cases = [
        make_input(horizon_days=20.0),
        make_input(lead_time_days="1"),
        make_input(po_quantity="200"),
        make_input(initial_stock=math.nan),
        make_input(po_quantity=math.inf),
        make_input(demand=DemandSpec(type="D", demand_list=bad_dl)),
    ]
    for inp in cases:
        v = inp.validate()
        assert not v.ok
        run = run_scenario(inp, engine="both")
        assert run.baseline is not None and run.baseline.needs_input
        assert run.stockpyl is not None and run.stockpyl.needs_input
        assert run.baseline.inventory_levels == []
        assert run.failures


# ---- §3 Stockpyl adapter：殘留訂單簿記 + 1e12 冒充無限的到貨可用時間錯位 ----

def test_stockpyl_bookkeeping_single_po_no_residual_orders():
    """簿記核對：FG policy 訂單只在 t=0 一筆（與 override 同步）、期末 pending/raw 歸零。

    同時核對實體到貨（po_arrivals）、可用庫存（inventory_levels vs baseline）。
    舊實作 FG 訂單每期 200、期末 pending_finished_goods=3800（未揭露的政策簿記）。
    """
    for lead in (0, 1, 2, 3):
        inp = make_input(lead_time_days=lead)
        sp = stockpyl_simulation(inp)
        bk = sp.order_bookkeeping
        assert bk is not None and len(bk) == inp.horizon_days, f"lead={lead}: 缺逐期簿記"
        assert bk[0]["order_quantity_fg"] == pytest.approx(200.0, abs=TOL), f"lead={lead}: t=0 FG 訂單應為 Q"
        assert all(b["order_quantity_fg"] == 0.0 for b in bk[1:]), f"lead={lead}: t>0 不得有殘留 FG 訂單"
        assert bk[-1]["pending_finished_goods"] == pytest.approx(0.0, abs=TOL), f"lead={lead}: 期末 pending 非零"
        assert bk[-1]["raw_material_inventory"] == pytest.approx(0.0, abs=TOL), f"lead={lead}: 期末 raw 非零"
        assert sp.po_arrivals == [[lead, 200.0]], f"lead={lead}: 到貨事件錯誤 {sp.po_arrivals}"
        assert sp.inventory_levels == deterministic_baseline(inp).inventory_levels, f"lead={lead}: 可用庫存錯位"


@pytest.mark.parametrize("lead", [1, 2, 3])
def test_stockpyl_large_initial_stock_matches_baseline(lead):
    """GPT-6 大 initial_stock 反例：到貨期當期可用庫存必須立即含 Q（不得晚一期）。

    舊實作（reorder_point=1e12 冒充無限）：initial_stock=1000000000100 時 t=lead 可用
    庫存少 200（t=lead+1 才轉 FG），total_cost 少 200；po_arrivals 卻仍記 t=lead 到貨。
    """
    inp = make_input(lead_time_days=lead, initial_stock=1000000000100.0)
    base = deterministic_baseline(inp)
    sp = stockpyl_simulation(inp)
    assert sp.po_arrivals == [[lead, 200.0]], f"lead={lead}: 到貨事件 {sp.po_arrivals}"
    assert sp.inventory_levels == base.inventory_levels, (
        f"lead={lead}: 可用庫存錯位\n  base={base.inventory_levels[:4]}\n  sp  ={sp.inventory_levels[:4]}"
    )
    # 手算：I0=1000000000100，t<lead 每期 −10，t=lead +200
    expected_arrival_il = 1000000000300.0 - 10.0 * (lead + 1)
    assert base.inventory_levels[lead] == expected_arrival_il
    assert sp.inventory_levels[lead] == expected_arrival_il
    assert sp.total_cost == base.total_cost
    bk = sp.order_bookkeeping
    assert bk[0]["order_quantity_fg"] == pytest.approx(200.0, abs=TOL)
    assert all(b["order_quantity_fg"] == 0.0 for b in bk[1:])
    assert bk[-1]["pending_finished_goods"] == pytest.approx(0.0, abs=TOL)
    assert bk[-1]["raw_material_inventory"] == pytest.approx(0.0, abs=TOL)
    cmp = compare_results(base, sp)
    assert cmp["inventory_levels"]["match"] is True
    assert cmp["total_cost"]["match"] is True


def test_baseline_result_has_no_order_bookkeeping():
    """order_bookkeeping 為 Stockpyl adapter 專屬簿記；baseline 必須為 None。"""
    res = deterministic_baseline(make_input())
    assert res.order_bookkeeping is None


# ──────────────────────────────────────────────────────────────────────────
# 第三修正回合新增（父 Agent 獨立探針反例，2026-09-13；probe exit=1）：
#  - 超大 int（10**10000）→ _is_finite_number 不得拋 OverflowError，回 False → needs_input
#  - demand=object() → validate 不得拋 AttributeError，回 needs_input=["demand"]
#  - demand_list=123 → validate 不得拋 TypeError，回 needs_input=["demand"]
#  - compare_results inventory_levels=None/非 iterable/不合約容器 → match=False、
#    max_abs_diff=None、reason 明列欄位與原因，不得拋 TypeError
# ──────────────────────────────────────────────────────────────────────────

def test_validation_huge_int_po_quantity_needs_input_not_overflow():
    """po_quantity=10**10000 → needs_input；validate 不得拋 OverflowError。"""
    inp = make_input(po_quantity=10**10000)
    v = inp.validate()  # 舊實作在此拋 OverflowError（math.isfinite 轉 float）
    assert not v.ok
    assert "po_quantity" in v.needs_input
    run = run_scenario(inp, engine="both")
    assert run.baseline is not None and "po_quantity" in run.baseline.needs_input
    assert run.stockpyl is not None and "po_quantity" in run.stockpyl.needs_input
    assert run.baseline.inventory_levels == []


def test_validation_huge_int_initial_stock_needs_input_not_overflow():
    """initial_stock=10**10000 → needs_input；validate 不得拋 OverflowError。"""
    inp = make_input(initial_stock=10**10000)
    v = inp.validate()  # 舊實作在此拋 OverflowError
    assert not v.ok
    assert "initial_stock" in v.needs_input


def test_validation_huge_int_holding_cost_needs_input_not_overflow():
    """holding_cost=10**10000 → needs_input；validate 不得拋 OverflowError。"""
    inp = make_input(holding_cost=10**10000)
    v = inp.validate()  # 舊實作在此拋 OverflowError
    assert not v.ok
    assert "holding_cost" in v.needs_input


def test_validation_demand_list_int_needs_input_not_typeerror():
    """demand_list=123（非 list）→ needs_input=["demand"]，validate 不得拋 TypeError。"""
    inp = make_input(demand=DemandSpec(type="D", demand_list=123))
    v = inp.validate()  # 舊實作在此拋 TypeError: object of type 'int' has no len()
    assert not v.ok
    assert "demand" in v.needs_input


def test_validation_demand_wrong_object_needs_input_not_attributeerror():
    """demand=object() → needs_input=["demand"]，validate 不得拋 AttributeError。"""
    inp = make_input(demand=object())
    v = inp.validate()  # 舊實作在此拋 AttributeError: 'object' object has no attribute 'type'
    assert not v.ok
    assert "demand" in v.needs_input


@pytest.mark.parametrize("bad", [None, 123, "ab"])
def test_comparator_inventory_wrong_container_fails_closed(bad):
    """inventory_levels 為 None、非 iterable（int）或不合約容器（str）→
    match=False、max_abs_diff=None、reason 明列欄位與原因，不得拋 TypeError。"""
    a = ScenarioResult(engine="baseline", inventory_levels=bad)
    b = ScenarioResult(engine="stockpyl", inventory_levels=[1.0])
    cmp = compare_results(a, b)  # 舊實作對 None/int 在此拋 TypeError
    assert cmp["inventory_levels"]["match"] is False
    assert cmp["inventory_levels"]["max_abs_diff"] is None
    assert "inventory_levels" in cmp["reason"]


# ──────────────────────────────────────────────────────────────────────────
# 第四修正回合（2026-09-14，reviewer 獨立重現之剩餘缺口）：
#  §1 comparator 容器契約：inventory_levels 只接受契約規定的數值 list；
#     po_arrivals 驗證容器、每筆二欄結構、期數型別/範圍與有限數量。
#     非法值（dict、None、NaN、inf、錯誤 tuple/結構）→ 結構化 mismatch，
#     reason 指出欄位/位置；不得 TypeError，也不得宣稱 engines agree。
# ──────────────────────────────────────────────────────────────────────────

def test_comparator_dict_inventory_levels_fails_closed():
    """dict inventory_levels 不得被轉成鍵列表假一致（reviewer 反例 A）。"""
    a = ScenarioResult(engine="baseline", inventory_levels=[1.0])
    b = ScenarioResult(engine="stockpyl", inventory_levels={1.0: "not a numerical inventory"})
    cmp = compare_results(a, b)
    assert cmp["inventory_levels"]["match"] is False
    assert cmp["inventory_levels"]["max_abs_diff"] is None
    assert cmp["inventory_levels"]["stockpyl"] is None  # 不得 list(dict) 成 [1.0]
    assert "inventory_levels" in cmp["reason"]
    assert "dict" in cmp["reason"]
    assert not cmp["reason"].startswith("engines agree")


def test_comparator_po_arrivals_inf_fails_closed_even_both_sides_equal():
    """雙邊相同 [[0, inf]]（inf==inf）不得宣稱一致（reviewer 反例 B）。"""
    a = ScenarioResult(engine="baseline", inventory_levels=[1.0], po_arrivals=[[0, math.inf]])
    b = copy.deepcopy(a)
    cmp = compare_results(a, b)
    assert cmp["po_arrivals"]["match"] is False
    assert "po_arrivals" in cmp["reason"]


def test_comparator_po_arrivals_nan_fails_closed_even_both_sides_equal():
    """雙邊 deepcopy 相同 [[0, nan]] 不得宣稱一致（deepcopy 對 atomic float 回同一
    物件、list 相等性 identity shortcut 使 nan==nan 成立 → 舊實作假一致）。"""
    a = ScenarioResult(engine="baseline", inventory_levels=[1.0], po_arrivals=[[0, math.nan]])
    b = copy.deepcopy(a)
    cmp = compare_results(a, b)
    assert cmp["po_arrivals"]["match"] is False
    assert "po_arrivals" in cmp["reason"]


def test_comparator_po_arrivals_none_fails_closed_without_raise():
    """po_arrivals=None → 結構化 mismatch，不得拋 TypeError（reviewer 反例）。"""
    a = ScenarioResult(engine="baseline", inventory_levels=[1.0], po_arrivals=None)
    b = ScenarioResult(engine="stockpyl", inventory_levels=[1.0], po_arrivals=[[0, 2.0]])
    cmp = compare_results(a, b)  # 舊實作在此拋 TypeError: 'NoneType' object is not iterable
    assert cmp["po_arrivals"]["match"] is False
    assert "po_arrivals" in cmp["reason"]


@pytest.mark.parametrize("bad_entry", [
    (0, 1.0, 2.0),   # 三欄 tuple（錯誤 tuple）
    (0,),            # 一欄
    ["a", 1.0],      # 期數非數值
    [1.5, 1.0],      # 期數非整數值
    [-1, 1.0],       # 期數為負
    {0: 1.0},        # dict 欄位
])
def test_comparator_po_arrivals_bad_structure_fails_closed(bad_entry):
    """錯誤二欄結構：即使雙邊完全相同也不得宣稱一致；reason 列出位置。"""
    a = ScenarioResult(engine="baseline", inventory_levels=[1.0], po_arrivals=[bad_entry])
    b = copy.deepcopy(a)
    cmp = compare_results(a, b)
    assert cmp["po_arrivals"]["match"] is False
    assert "po_arrivals" in cmp["reason"]
    assert "po_arrivals[0]" in cmp["reason"]


def test_comparator_po_arrivals_valid_pairs_still_match():
    """合法二欄 list（[[0, 2.0]] 雙側相同）仍可比對一致（回歸 pin）。"""
    a = ScenarioResult(engine="baseline", inventory_levels=[1.0], po_arrivals=[[0, 2.0]])
    b = ScenarioResult(engine="stockpyl", inventory_levels=[1.0], po_arrivals=[[0, 2.0]])
    cmp = compare_results(a, b)
    assert cmp["po_arrivals"]["match"] is True


# ──────────────────────────────────────────────────────────────────────────
#  §2 validation 錯誤訊息不得再次崩潰：有界、安全的值描述，不因 repr 超過
#     int 字串轉換上限拋例外；run_scenario fail-closed、needs_input 含 demand、
#     兩引擎回空數值結果。不得提高全域 int 限制。
# ──────────────────────────────────────────────────────────────────────────

def test_validation_huge_int_demand_entry_safe_message_and_needs_input():
    """demand_list=[10**10000]：validate 不得在訊息建構時拋例外（reviewer 反例）。"""
    inp = ScenarioInput(
        horizon_days=1,
        po_id="PO",
        po_quantity=1.0,
        initial_stock=2.0,
        lead_time_days=0,
        demand=DemandSpec(type="D", demand_list=[10**10000]),
    )
    v = inp.validate()  # 舊實作在此拋 ValueError（Exceeds the limit (4300 digits)）
    assert not v.ok
    assert "demand" in v.needs_input
    assert any("demand_list[0]" in i for i in v.issues)
    run = run_scenario(inp, engine="both")
    assert run.baseline is not None and "demand" in run.baseline.needs_input
    assert run.stockpyl is not None and "demand" in run.stockpyl.needs_input
    assert run.baseline.inventory_levels == []
    assert run.stockpyl.inventory_levels == []
    assert run.baseline.total_cost is None
    assert run.stockpyl.total_cost is None
    assert any("missing required input" in f for f in run.failures)


def test_validation_huge_int_demand_type_safe_message():
    """demand.type=10**10000（未知型別分支）→ 安全描述，validate 不拋例外。"""
    inp = make_input(demand=DemandSpec(type=10**10000))
    v = inp.validate()  # 舊實作在此拋 ValueError（{d.type!r}）
    assert not v.ok
    assert "demand" in v.needs_input
    assert any("未知 demand type" in i for i in v.issues)


def test_validation_huge_int_lead_time_safe_message():
    """lead_time_days=10**10000 → needs_input 且訊息安全（回歸 pin：既有分支未嵌值）。"""
    inp = make_input(lead_time_days=10**10000)
    v = inp.validate()
    assert not v.ok
    assert "lead_time_days" in v.needs_input


# ──────────────────────────────────────────────────────────────────────────
#  §3 運算後非有限：輸入有限但乘法/累計得 NaN/inf（例 holding_cost=1e308 →
#     total_cost=inf）不得 failures=[] 留下成功外觀；回清楚 numerical failure。
# ──────────────────────────────────────────────────────────────────────────

def test_overflow_cost_fails_closed_numerical_failure():
    """holding_cost=1e308 → total_cost=inf：run 不得回報無失敗（reviewer 反例）。"""
    inp = make_input(
        horizon_days=1,
        lead_time_days=0,
        initial_stock=2.0,
        po_quantity=1.0,
        po_id="PO",
        seed=0,
        demand=DemandSpec(type="D", demand_list=[1.0]),
        holding_cost=1e308,
        stockout_cost=1.0,
    )
    assert inp.validate().ok  # 輸入全部有限 → 驗證通過；非有限來自運算
    run = run_scenario(inp, engine="both")
    assert run.baseline is None  # 不得回傳 total_cost=inf 的成功結果
    assert run.stockpyl is None
    assert run.failures  # 不得 failures=[]
    assert any("numerical failure" in f for f in run.failures)
    with pytest.raises(ValueError, match="numerical failure"):
        deterministic_baseline(inp)


# ──────────────────────────────────────────────────────────────────────────
#  §4 rejected input 可保存：initial_stock/po_quantity/demand 含 NaN、inf、
#     10**10000 等被契約拒絕時，persist_run 仍寫標準 JSON 失敗紀錄，明確標示
#     欄位、原值類型/拒絕原因、版本與 seed；不猜成數值、不用 allow_nan=True；
#     讀回 canonical 位元組穩定。
# ──────────────────────────────────────────────────────────────────────────

def test_persist_rejected_nan_initial_stock_saves_standard_json(tmp_path):
    """initial_stock=nan 被拒絕後：persist 寫標準 JSON、rejected 標記、讀回位元組穩定。"""
    inp = make_input(initial_stock=math.nan)
    run = run_scenario(inp, engine="both")
    assert run.failures
    assert run.baseline.needs_input == ["initial_stock"]
    path = persist_run(run, str(tmp_path))  # 舊實作在此拋 ValueError: Out of range float
    text = pathlib.Path(path).read_text(encoding="utf-8")
    # 標準 JSON：拒絕任何裸 NaN/Infinity/-Infinity token（字串 "nan" 是合法標記值）。
    payload = json.loads(text, parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
    assert payload["engine_version"] == SCENARIO_ENGINE_VERSION
    assert payload["inputs"]["seed"] == 0
    assert payload["inputs"]["input_version"] == 1
    mark = payload["inputs"]["initial_stock"]
    assert mark["rejected"] is True
    assert mark["field"] == "initial_stock"
    assert mark["original_type"] == "float"
    assert mark["original_value"] == "nan"
    assert mark["reason"]
    assert payload["failures"]  # 失敗項目隨檔保存
    loaded = load_run(path)
    assert loaded.inputs.initial_stock == mark
    assert loaded.failures == run.failures
    assert loaded.baseline.canonical_bytes() == run.baseline.canonical_bytes()
    assert loaded.stockpyl.canonical_bytes() == run.stockpyl.canonical_bytes()
    # 讀回後再次保存 → 位元組一致（canonical 穩定）
    path2 = persist_run(loaded, str(pathlib.Path(tmp_path) / "r2"))
    assert pathlib.Path(path2).read_bytes() == text.encode("utf-8")


def test_persist_rejected_inf_po_quantity_saves_standard_json(tmp_path):
    inp = make_input(po_quantity=math.inf)
    run = run_scenario(inp, engine="both")
    path = persist_run(run, str(tmp_path))
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    mark = payload["inputs"]["po_quantity"]
    assert mark["rejected"] is True
    assert mark["field"] == "po_quantity"
    assert mark["original_type"] == "float"
    assert mark["original_value"] == "inf"
    assert payload["inputs"]["seed"] == 0
    assert payload["engine_version"] == SCENARIO_ENGINE_VERSION
    loaded = load_run(path)
    assert loaded.inputs.po_quantity == mark


def test_persist_rejected_huge_int_po_quantity_saves_standard_json(tmp_path):
    """po_quantity=10**10000 被拒絕後仍可保存：不得猜成數值、不得 json 拋例外。"""
    inp = make_input(po_quantity=10**10000)
    run = run_scenario(inp, engine="both")
    path = persist_run(run, str(tmp_path))  # 舊實作 json.dumps 在此拋 ValueError
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    mark = payload["inputs"]["po_quantity"]
    assert mark["rejected"] is True
    assert mark["field"] == "po_quantity"
    assert mark["original_type"] == "int"
    assert mark["reason"]
    assert payload["inputs"]["seed"] == 0
    assert payload["engine_version"] == SCENARIO_ENGINE_VERSION
    loaded = load_run(path)
    assert loaded.inputs.po_quantity == mark


def test_persist_rejected_inf_demand_entry_saves_standard_json(tmp_path):
    """demand_list=[inf] 被拒絕後仍可保存：逐項標記欄位位置。"""
    inp = make_input(
        horizon_days=1,
        lead_time_days=0,
        demand=DemandSpec(type="D", demand_list=[math.inf]),
    )
    run = run_scenario(inp, engine="both")
    path = persist_run(run, str(tmp_path))
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    mark = payload["inputs"]["demand"]["demand_list"][0]
    assert mark["rejected"] is True
    assert mark["field"] == "demand.demand_list[0]"
    assert mark["original_type"] == "float"
    loaded = load_run(path)
    assert loaded.inputs.demand.demand_list[0] == mark
