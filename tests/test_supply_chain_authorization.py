"""Server-side authorization contracts for L2 supply-chain actions."""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from backend import database
from backend import access_control
from backend import supply_chain_news as news
from backend import supply_chain_risk as risk


@pytest.fixture
def supply_db(tmp_path, monkeypatch):
    db_path = tmp_path / "supply-authorization.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    monkeypatch.setattr(risk, "DB_FILE", str(db_path))
    database.init_db()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO inventory
                (product_id, name, stock, reorder_point,
                 baseline_reorder_point, daily_sales)
            VALUES ('P-AUTH', 'Authorization fixture', 50, 10, 8, 2)
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO suppliers
                (supplier_id, name, country, region, is_official)
            VALUES ('S-AUTH', 'Authorization supplier', 'Taiwan', 'Taichung', 1)
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO purchase_orders
                (po_id, supplier_id, status, estimated_delay_days,
                 alternative_suggestion)
            VALUES ('PO-AUTH', 'S-AUTH', 'pending', 1, 'original')
            """
        )
        conn.execute(
            """
            INSERT INTO purchase_order_items (po_id, product_id, qty, unit_price)
            VALUES ('PO-AUTH', 'P-AUTH', 5, 10)
            """
        )
        conn.execute(
            """
            INSERT INTO supply_chain_events
                (event_type, region, country, impact_days, description, created_at)
            VALUES ('delay', 'Taichung', 'Taiwan', 4, 'fixture', '2026-07-20 00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO risk_heatmap
                (region_key, display_name, latitude, longitude, risk_pct,
                 ai_summary, updated_at)
            VALUES ('Taiwan|Taichung', 'Taiwan Taichung', 24.15, 120.68,
                    25, 'fixture', '2026-07-20 00:00')
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO esg_risk_factors
                (risk_type, risk_key, risk_score, weight, note, updated_at)
            VALUES ('region', 'fixture', 20, 1, 'fixture', '2026-07-20 00:00')
            """
        )
        conn.commit()
    return db_path


def _snapshot(db_path):
    tables = (
        "supply_chain_events",
        "risk_heatmap",
        "inventory",
        "purchase_orders",
        "esg_risk_factors",
    )
    with sqlite3.connect(db_path) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }


def _mutation(name: str, actor: str | None):
    event_id = risk.get_risk_events_list(limit=1).iloc[0]["id"]
    factor_id = risk.get_risk_factors().iloc[0]["id"]
    operations = {
        "add_event": lambda: risk.add_risk_event(
            "strike", "Kaohsiung", "Taiwan", 7, "new", actor=actor
        ),
        "delete_event": lambda: risk.delete_risk_event(event_id, actor=actor),
        "upsert_heatmap": lambda: risk.upsert_risk_heatmap(
            "Japan|Tokyo", "Japan Tokyo", 35.68, 139.69, 80,
            "new", actor=actor
        ),
        "reset_heatmap": lambda: risk.reset_risk_heatmap_to_initial(actor=actor),
        "apply_heatmap": lambda: risk.apply_heatmap_updates(
            [{"display_name": "Taiwan Taichung", "risk_pct": 90}],
            "new", actor=actor
        ),
        "update_po_impact": lambda: risk.update_po_impact(
            "PO-AUTH", 9, "replacement", actor=actor
        ),
        "increase_stock": lambda: risk.increase_safety_stock_for_event(
            "Taichung", "Taiwan", 5, actor=actor
        ),
        "restore_stock": lambda: risk.restore_all_rop_to_baseline(actor=actor),
        "update_rop": lambda: risk.update_reorder_point("P-AUTH", 99, actor=actor),
        "save_factor": lambda: risk.save_risk_factor(
            "region", "new", 70, 1, actor=actor
        ),
        "delete_factor": lambda: risk.delete_risk_factor(factor_id, actor=actor),
        "clear_factors": lambda: risk.clear_all_risk_factors(actor=actor),
        "load_presets": lambda: risk.load_preset_risk_factors(actor=actor),
    }
    return operations[name]()


_WORKSPACE_MUTATIONS = (
    "add_event",
    "delete_event",
    "upsert_heatmap",
    "reset_heatmap",
    "apply_heatmap",
    "save_factor",
    "delete_factor",
    "clear_factors",
    "load_presets",
)

_ERP_POLICY_MUTATIONS = (
    "update_po_impact",
    "increase_stock",
    "restore_stock",
    "update_rop",
)

_MUTATIONS = _WORKSPACE_MUTATIONS + _ERP_POLICY_MUTATIONS


def test_supply_capabilities_separate_workspace_from_erp_policy():
    workspace_write = access_control.RISK_WORKSPACE_WRITE
    erp_policy_write = access_control.ERP_POLICY_WRITE

    planner = access_control.capabilities_for_role("supply_planner")
    warehouse = access_control.capabilities_for_role("warehouse")
    admin = access_control.capabilities_for_role("admin")
    viewer = access_control.capabilities_for_role("risk_viewer")
    approver = access_control.capabilities_for_role("procurement_approver")

    assert workspace_write in planner
    assert erp_policy_write not in planner
    assert {workspace_write, erp_policy_write} <= warehouse
    assert {workspace_write, erp_policy_write} <= admin
    assert workspace_write not in viewer | approver
    assert erp_policy_write not in viewer | approver


@pytest.mark.parametrize("actor", [None, "viewer", "approver"])
@pytest.mark.parametrize("operation", _MUTATIONS)
def test_l2_mutations_deny_before_any_database_side_effect(
    supply_db, operation, actor
):
    before = _snapshot(supply_db)

    with pytest.raises(PermissionError):
        _mutation(operation, actor)

    assert _snapshot(supply_db) == before


@pytest.mark.parametrize("actor", ["planner", "admin", "wh1"])
@pytest.mark.parametrize("operation", _WORKSPACE_MUTATIONS)
def test_authorized_l2_roles_can_write_workspace(
    supply_db, operation, actor
):
    _mutation(operation, actor)


@pytest.mark.parametrize("actor", ["admin", "wh1"])
@pytest.mark.parametrize("operation", _ERP_POLICY_MUTATIONS)
def test_authorized_erp_policy_roles_can_execute_mutation(
    supply_db, operation, actor
):
    _mutation(operation, actor)


@pytest.mark.parametrize("operation", _ERP_POLICY_MUTATIONS)
def test_planner_cannot_write_erp_policy_and_has_no_database_side_effect(
    supply_db, operation
):
    before = _snapshot(supply_db)

    with pytest.raises(PermissionError):
        _mutation(operation, "planner")

    assert _snapshot(supply_db) == before


@pytest.mark.parametrize("actor", [None, "viewer", "approver"])
def test_what_if_denies_before_sensitive_reads_or_llm(
    supply_db, monkeypatch, actor
):
    calls = {"read": 0, "llm": 0}

    def forbidden_read(*args, **kwargs):
        calls["read"] += 1
        raise AssertionError("sensitive ERP data was read before authorization")

    def forbidden_llm(*args, **kwargs):
        calls["llm"] += 1
        raise AssertionError("LLM was called before authorization")

    monkeypatch.setattr(risk, "__pd_read", forbidden_read)
    monkeypatch.setattr("backend.llm_client.complete_text", forbidden_llm)

    with pytest.raises(PermissionError):
        risk.what_if_simulation("", "What happens?", actor=actor)

    assert calls == {"read": 0, "llm": 0}


@pytest.mark.parametrize("actor", ["planner", "admin", "wh1"])
def test_authorized_roles_can_run_what_if(supply_db, monkeypatch, actor):
    llm_calls = []

    def fake_llm(*args, **kwargs):
        llm_calls.append((args, kwargs))
        return "authorized result"

    monkeypatch.setattr("backend.llm_client.complete_text", fake_llm)

    assert risk.what_if_simulation(
        "", "What happens?", actor=actor
    ) == "authorized result"
    assert len(llm_calls) == 1


def test_entitlement_revocation_is_immediate(supply_db, monkeypatch):
    llm_calls = []

    def fake_llm(*args, **kwargs):
        llm_calls.append((args, kwargs))
        return "authorized result"

    monkeypatch.setattr("backend.llm_client.complete_text", fake_llm)

    assert risk.what_if_simulation(
        "", "What happens?", actor="planner"
    ) == "authorized result"

    database.run_query(
        "UPDATE organization_entitlements SET enabled = 0 "
        "WHERE organization_id = 'demo-org' AND entitlement_key = 'l2_decision'",
        fetch=False,
    )

    before = _snapshot(supply_db)
    with pytest.raises(PermissionError):
        risk.add_risk_event(
            "strike", "Kaohsiung", "Taiwan", 7, "revoked",
            actor="planner"
        )
    with pytest.raises(PermissionError):
        risk.what_if_simulation("", "Try again", actor="planner")

    assert _snapshot(supply_db) == before
    assert len(llm_calls) == 1


@pytest.mark.parametrize("actor", [None, "viewer", "approver"])
def test_news_refresh_denies_before_fetch_or_database_write(
    supply_db, monkeypatch, actor
):
    fetch_calls = []

    def forbidden_fetch(*args, **kwargs):
        fetch_calls.append((args, kwargs))
        raise AssertionError("external news fetch ran before authorization")

    monkeypatch.setattr(news, "fetch_country_news", forbidden_fetch)
    before = _snapshot(supply_db)
    with sqlite3.connect(supply_db) as conn:
        news_count = conn.execute(
            "SELECT COUNT(*) FROM supply_chain_news"
        ).fetchone()[0]

    with pytest.raises(PermissionError):
        news.refresh_news_for_countries(["Taiwan"], actor=actor)

    with sqlite3.connect(supply_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM supply_chain_news"
        ).fetchone()[0] == news_count
    assert _snapshot(supply_db) == before
    assert fetch_calls == []


def test_planner_news_refresh_applies_heatmap_update(supply_db, monkeypatch):
    monkeypatch.setattr("backend.llm_client.llm_available", lambda: True)
    monkeypatch.setattr(
        news,
        "fetch_country_news",
        lambda *args, **kwargs: [
            {
                "country": "Taiwan",
                "region": "Taichung",
                "title": "Port disruption",
                "summary": "Delay expected",
                "url": "https://example.test/news",
                "source": "test",
                "published_at": "2026-07-20 00:00",
                "relevance_tag": "supply_chain",
            }
        ],
    )
    monkeypatch.setattr(
        risk,
        "batch_infer_affected_region_from_news",
        lambda **kwargs: [
            {
                "is_relevant": True,
                "estimated_delay": 5,
                "event_type": "delay",
                "country": "Taiwan",
                "region": "Taichung",
                "chinese_summary": "Test summary",
            }
        ],
    )
    monkeypatch.setattr(
        risk,
        "get_heatmap_ai_summary",
        lambda **kwargs: (
            "Authorized update",
            [{"display_name": "Taiwan Taichung", "risk_pct": 88}],
            [],
        ),
    )

    result = news.refresh_news_for_countries(["Taiwan"], actor="planner")

    assert result["saved_count"] == 1
    with sqlite3.connect(supply_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM supply_chain_news"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT risk_pct FROM risk_heatmap "
            "WHERE region_key = 'Taiwan|Taichung'"
        ).fetchone()[0] == 88


def test_news_refresh_does_not_swallow_midflight_authorization_failure(
    supply_db, monkeypatch
):
    monkeypatch.setattr("backend.llm_client.llm_available", lambda: True)
    monkeypatch.setattr(news, "fetch_country_news", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        risk,
        "get_heatmap_ai_summary",
        lambda **kwargs: (
            "Update",
            [{"display_name": "Taiwan Taichung", "risk_pct": 88}],
            [],
        ),
    )

    def revoked_during_refresh(*args, **kwargs):
        raise PermissionError("entitlement was revoked")

    monkeypatch.setattr(risk, "apply_heatmap_updates", revoked_during_refresh)

    with pytest.raises(PermissionError, match="revoked"):
        news.refresh_news_for_countries(["Taiwan"], actor="planner")


def test_supply_components_require_and_forward_live_actor():
    component_paths = (
        Path("frontend/components/supply_map.py"),
        Path("frontend/components/risk_dashboard.py"),
    )
    required_renderers = {
        "render_risk_shortcuts",
        "render_supply_chain_map",
        "render_what_if_analysis",
        "render_intelligence_gathering",
        "render_response_execution",
    }
    protected_calls = {
        "add_risk_event",
        "delete_risk_event",
        "upsert_risk_heatmap",
        "reset_risk_heatmap_to_initial",
        "apply_heatmap_updates",
        "update_po_impact",
        "what_if_simulation",
        "increase_safety_stock_for_event",
        "restore_all_rop_to_baseline",
        "update_reorder_point",
        "save_risk_factor",
        "delete_risk_factor",
        "clear_all_risk_factors",
        "load_preset_risk_factors",
        "refresh_news_for_countries",
    }

    found_renderers = set()
    protected_invocations = []
    for path in component_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in required_renderers:
                    found_renderers.add(node.name)
                    parameter_names = {
                        argument.arg
                        for argument in (
                            node.args.posonlyargs
                            + node.args.args
                            + node.args.kwonlyargs
                        )
                    }
                    assert "actor" in parameter_names
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in protected_calls
            ):
                protected_invocations.append((path, node))
                assert any(keyword.arg == "actor" for keyword in node.keywords), (
                    f"{path}:{node.lineno} must pass actor to {node.func.id}"
                )

    assert found_renderers == required_renderers
    assert protected_invocations


def test_planner_component_hides_direct_erp_policy_actions(supply_db):
    from frontend.components import risk_dashboard

    assert not risk_dashboard.can_write_erp_policy("planner")
    assert not risk_dashboard.can_write_erp_policy("viewer")
    assert not risk_dashboard.can_write_erp_policy("approver")
    assert risk_dashboard.can_write_erp_policy("admin")
    assert risk_dashboard.can_write_erp_policy("wh1")

    path = Path("frontend/components/risk_dashboard.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    policy_calls = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id
            in {
                "increase_safety_stock_for_event",
                "restore_all_rop_to_baseline",
                "update_reorder_point",
            }
        ):
            continue
        policy_calls.append(node)
        cursor = node
        guarded = False
        while cursor in parents:
            cursor = parents[cursor]
            if (
                isinstance(cursor, ast.If)
                and "can_write_policy" in ast.unparse(cursor.test)
            ):
                guarded = True
                break
        assert guarded, (
            f"{path}:{node.lineno} ERP policy mutation must be hidden "
            "behind can_write_policy"
        )

    assert policy_calls
    assert "受治理提案" in source
