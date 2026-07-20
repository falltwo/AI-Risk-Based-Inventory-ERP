import pandas as pd
import pytest

from backend import scheduler


def test_scheduler_refresh_passes_configured_actor(monkeypatch):
    observed = {}
    monkeypatch.setattr(
        scheduler,
        "require_capability",
        lambda actor, capability: observed.update(
            authorized=(actor, capability)
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "get_suppliers_for_map",
        lambda: pd.DataFrame({"country": ["台灣", "日本", "台灣"]}),
    )

    def fake_refresh(countries, **kwargs):
        observed["countries"] = countries
        observed.update(kwargs)
        return {"saved": 2}

    monkeypatch.setattr(scheduler, "refresh_news_for_countries", fake_refresh)

    result = scheduler.refresh_supply_chain_news_once(actor="planner")

    assert result == {"saved": 2}
    assert observed == {
        "authorized": ("planner", scheduler.RISK_WORKSPACE_WRITE),
        "countries": ["台灣", "日本"],
        "max_per_country": 5,
        "actor": "planner",
    }


def test_scheduler_refresh_requires_actor(monkeypatch):
    monkeypatch.setattr(
        scheduler, "get_suppliers_for_map", lambda: pytest.fail("must not read")
    )

    with pytest.raises(PermissionError, match="ERP_SCHEDULER_ACTOR"):
        scheduler.refresh_supply_chain_news_once(actor="")


def test_scheduler_rejects_unauthorized_actor_before_supplier_read(
    tmp_path, monkeypatch
):
    from backend import database

    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "scheduler.db"))
    database.init_db()
    monkeypatch.setattr(
        scheduler, "get_suppliers_for_map", lambda: pytest.fail("must not read")
    )

    with pytest.raises(PermissionError, match="權限"):
        scheduler.refresh_supply_chain_news_once(actor="viewer")


def test_scheduler_stays_disabled_without_service_identity(monkeypatch):
    monkeypatch.setattr(scheduler, "_scheduler_started", False)
    monkeypatch.delenv("ERP_SCHEDULER_ACTOR", raising=False)

    assert scheduler.start_background_jobs() is False
    assert scheduler._scheduler_started is False
