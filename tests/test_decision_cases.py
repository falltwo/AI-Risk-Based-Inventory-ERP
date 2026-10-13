"""Acceptance tests for the minimal Decision Case service (DAG-20260912-e15d763a).

Scope record: this task only delivers case persistence (events, exposure
mappings, append-only input versions, scenario run references) with the
existing capability contract.  Proposal / approval / receipt integration is
NOT connected yet and no first-task scenario engine is imported.

Every test uses an explicitly passed, isolated SQLite connection.  No test
depends on Streamlit sessions or on the global production database.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from backend.access_control import (
    L1_MONITOR,
    L2_DECISION,
    L3_GOVERNED_ACTION,
    RISK_ANALYSIS_READ,
    RISK_WORKSPACE_WRITE,
    AccessContext,
    load_principal,
)
from backend.decision_case_repository import (
    DecisionCaseDataError,
    init_schema,
    insert_version as repo_insert_version,
)
from backend.decision_cases import (
    KIND_MANUAL_CORRECTION,
    KIND_RAW_EVIDENCE,
    KIND_SCENARIO_INPUT,
    STATUS_CANDIDATE,
    STATUS_CONFIRMED,
    STATUS_UNKNOWN,
    STATUS_UNMAPPED,
    VALID_MAPPING_STATUSES,
    CaseNotFoundError,
    DecisionCaseService,
    PermissionDeniedError,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "decision_cases"


def _load_fixture(name="synthetic_case_001.json") -> dict:
    with open(FIXTURE_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def case_db(tmp_path):
    """Isolated SQLite file.  The fixture closes its own connection so every
    test must open (and reopen) connections explicitly."""
    db_path = tmp_path / "decision-cases.db"
    conn = sqlite3.connect(db_path)
    try:
        init_schema(conn)
        _seed_auth_tables(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _open(db_path):
    conn = sqlite3.connect(db_path)
    init_schema(conn)  # idempotent; also re-enables foreign keys per connection
    return conn


def _seed_auth_tables(conn, org="org-syn-1"):
    """Seed the existing authorization tables (access_control contract) on an
    isolated connection so load_principal can resolve real DB-backed
    principals; no Streamlit, no login session."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users "
        "(username TEXT PRIMARY KEY, password TEXT, role TEXT, name TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_organizations "
        "(username TEXT PRIMARY KEY, organization_id TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS organization_entitlements "
        "(organization_id TEXT NOT NULL, entitlement_key TEXT NOT NULL, "
        "enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)), "
        "PRIMARY KEY (organization_id, entitlement_key))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_metadata "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO app_metadata VALUES "
        "('deployment_organization_id', ?)",
        (org,),
    )
    for username, role, name in (
        ("planner", "supply_planner", "規劃員"),
        ("viewer", "risk_viewer", "觀測員"),
        ("approver", "procurement_approver", "採購核准員"),
    ):
        conn.execute(
            "INSERT OR REPLACE INTO users VALUES (?, 'x', ?, ?)",
            (username, role, name),
        )
        conn.execute(
            "INSERT OR REPLACE INTO user_organizations VALUES (?, ?)",
            (username, org),
        )
    for entitlement in (L1_MONITOR, L2_DECISION, L3_GOVERNED_ACTION):
        conn.execute(
            "INSERT OR REPLACE INTO organization_entitlements VALUES (?, ?, 1)",
            (org, entitlement),
        )


def _principal(conn, username):
    """Resolve a DB-backed principal through the existing load_principal
    contract (fresh entitlements, deployment binding)."""
    principal = load_principal(username, conn=conn)
    assert principal is not None, f"測試授權資料缺少 {username}"
    return principal


def _service(conn, username="planner", *, auth_conn=None):
    principal = _principal(auth_conn if auth_conn is not None else conn, username)
    return DecisionCaseService(conn=conn, principal=principal, auth_conn=auth_conn)


def _build_case_from_fixture(conn, fixture):
    """Build one case from the synthetic fixture through the service."""
    service = _service(conn)
    service.create_case(
        fixture["case_id"], fixture["title"], actor=fixture["created_by"]
    )
    for event in fixture["events"]:
        service.add_event(
            event_id=event["event_id"],
            case_id=fixture["case_id"],
            event_type=event["event_type"],
            region=event["region"],
            country=event["country"],
            description=event["description"],
            occurred_at=event["occurred_at"],
            actor=event["recorded_by"],
        )
    for mapping in fixture["mappings"]:
        service.add_mapping(
            mapping_id=mapping["mapping_id"],
            case_id=fixture["case_id"],
            subject_type=mapping["subject_type"],
            subject_key=mapping["subject_key"],
            status=mapping["status"],
            rationale=mapping["rationale"],
        )
    for version in fixture["versions"]:
        service.append_version(
            fixture["case_id"], version["kind"], dict(version["content"])
        )
    for run in fixture["runs"]:
        service.record_scenario_run(
            case_id=fixture["case_id"],
            run_id=run["run_id"],
            input_version=run["input_version"],
            result=dict(run["result"]),
        )
    return service


# ── 驗收 1：關閉並重新連線後，同一 case_id 仍可讀回事件、曝險與結果引用 ──

def test_case_survives_close_and_reconnect(case_db):
    fixture = _load_fixture()

    conn = _open(case_db)
    try:
        _build_case_from_fixture(conn, fixture)
        conn.commit()
    finally:
        conn.close()

    conn2 = _open(case_db)
    try:
        service = _service(conn2)
        case = service.get_case(fixture["case_id"])
        assert case is not None
        assert case.title == fixture["title"]
        assert case.case_id == fixture["case_id"]

        events = service.list_events(fixture["case_id"])
        assert [e.event_id for e in events] == ["EV-SYN-001"]
        assert events[0].event_type == "port_disruption"

        mappings = service.list_mappings(fixture["case_id"])
        assert {m.mapping_id for m in mappings} == {
            "MAP-SYN-001",
            "MAP-SYN-002",
            "MAP-SYN-003",
            "MAP-SYN-004",
        }

        runs = service.list_runs(fixture["case_id"])
        assert {r.run_id for r in runs} == {"RUN-SYN-001", "RUN-SYN-002"}
        by_id = {r.run_id: r for r in runs}
        assert by_id["RUN-SYN-001"].result == {"affected_suppliers": 1, "score": 42.0}
        assert by_id["RUN-SYN-002"].result == {"affected_suppliers": 3, "score": 71.0}
    finally:
        conn2.close()


# ── 驗收 2：原始證據、人工修正與 scenario 輸入各自保留版本 ──

def test_versions_are_append_only_and_never_overwrite(case_db):
    conn = _open(case_db)
    try:
        service = _service(conn)
        service.create_case("CASE-V-001", "版本測試案例")
        v1 = {"headline": "第一版證據"}
        v2 = {"headline": "第二版證據"}

        service.append_version("CASE-V-001", KIND_RAW_EVIDENCE, v1)
        service.append_version("CASE-V-001", KIND_RAW_EVIDENCE, v2)
        service.append_version("CASE-V-001", KIND_MANUAL_CORRECTION, {"note": "修正"})

        raw = service.list_versions("CASE-V-001", KIND_RAW_EVIDENCE)
        assert [(v.version, v.content) for v in raw] == [(1, v1), (2, v2)]

        correction = service.list_versions("CASE-V-001", KIND_MANUAL_CORRECTION)
        # 每種 kind 各自編版：修正只有第 1 版
        assert [v.version for v in correction] == [1]

        # 再追加一版後，歷史內容仍逐字相同
        service.append_version(
            "CASE-V-001", KIND_RAW_EVIDENCE, {"headline": "第三版證據"}
        )
        raw_again = service.list_versions("CASE-V-001", KIND_RAW_EVIDENCE)
        assert raw_again[0].content == v1
        assert raw_again[1].content == v2
        assert [v.version for v in raw_again] == [1, 2, 3]
        assert service.latest_version("CASE-V-001", KIND_RAW_EVIDENCE) == 3
        conn.commit()
    finally:
        conn.close()


# ── 驗收 3：輸入版本更新後舊 run 永久 stale ──

def test_stale_is_irreversible_version_relation(case_db):
    conn = _open(case_db)
    try:
        service = _service(conn)
        service.create_case("CASE-S-001", "stale 語意測試")
        service.append_version("CASE-S-001", KIND_SCENARIO_INPUT, {"v": 1})
        service.record_scenario_run("CASE-S-001", "RUN-S-001", 1, {"score": 10})

        assert service.list_runs("CASE-S-001")[0].stale is False

        service.append_version("CASE-S-001", KIND_SCENARIO_INPUT, {"v": 2})
        runs = service.list_runs("CASE-S-001")
        assert runs[0].stale is True, "輸入更新後，舊 run 必須 stale"

        # 新版本 run 不會讓舊版本 run 恢復有效
        service.record_scenario_run("CASE-S-001", "RUN-S-002", 2, {"score": 20})
        by_id = {r.run_id: r for r in service.list_runs("CASE-S-001")}
        assert by_id["RUN-S-002"].stale is False
        assert by_id["RUN-S-001"].stale is True, "舊版本 run 不得因新 run 而復活"

        # 再更新一版：兩個舊 run 都 stale（不可逆）
        service.append_version("CASE-S-001", KIND_SCENARIO_INPUT, {"v": 3})
        by_id = {r.run_id: r for r in service.list_runs("CASE-S-001")}
        assert by_id["RUN-S-001"].stale is True
        assert by_id["RUN-S-002"].stale is True
        conn.commit()
    finally:
        conn.close()


# ── 驗收 4：確定／候選／未知可區分；未命中保持 unknown／unmapped ──

def test_mapping_statuses_distinct_and_miss_stays_unmapped(case_db):
    conn = _open(case_db)
    try:
        service = _service(conn)
        service.create_case("CASE-M-001", "曝險對映測試")
        for mapping_id, status in (
            ("MAP-1", STATUS_CONFIRMED),
            ("MAP-2", STATUS_CANDIDATE),
            ("MAP-3", STATUS_UNKNOWN),
            ("MAP-4", STATUS_UNMAPPED),
        ):
            service.add_mapping(
                mapping_id, "CASE-M-001", "supplier", f"SUBJ-{mapping_id[-1]}",
                status, "",
            )

        statuses = {m.status for m in service.list_mappings("CASE-M-001")}
        assert statuses == {
            STATUS_CONFIRMED,
            STATUS_CANDIDATE,
            STATUS_UNKNOWN,
            STATUS_UNMAPPED,
        }
        assert "safe" not in VALID_MAPPING_STATUSES, "未命中不得被標成 safe"

        # 未命中的 subject：查不到對映、不建立任何列、狀態維持 unmapped
        assert service.find_mapping("CASE-M-001", "supplier", "NO-SUCH") is None
        assert (
            service.mapping_status_for("CASE-M-001", "supplier", "NO-SUCH")
            == STATUS_UNMAPPED
        )
        assert len(service.list_mappings("CASE-M-001")) == 4

        # 非法狀態 fail closed，不會被自動正規化
        with pytest.raises(ValueError):
            service.add_mapping("MAP-BAD", "CASE-M-001", "supplier", "X", "safe", "")
        conn.commit()
    finally:
        conn.close()


# ── 驗收 5：讀寫皆走既有 capability 邊界，無有效 principal 遭拒 ──

def test_writes_require_workspace_write_capability(case_db):
    conn = _open(case_db)
    try:
        _service(conn).create_case("CASE-A-001", "先建好案件")
        conn.commit()

        # risk_viewer 只有 overview 讀取權：寫入遭拒
        with pytest.raises(PermissionDeniedError):
            _service(conn, "viewer").append_version(
                "CASE-A-001", KIND_RAW_EVIDENCE, {"x": 1}
            )
        # procurement_approver 也沒有 risk workspace 寫入權
        with pytest.raises(PermissionDeniedError):
            _service(conn, "approver").create_case("CASE-A-002", "越權案件")
        # principal 完全缺失
        with pytest.raises(PermissionDeniedError):
            DecisionCaseService(conn=conn, principal=None).create_case(
                "CASE-A-003", "無主案件"
            )
        # 空 capability 的 principal 同樣遭拒
        empty = AccessContext(
            username="ghost", role="risk_viewer", name="ghost",
            organization_id="org-syn-1",
            entitlements=frozenset(), capabilities=frozenset(),
        )
        with pytest.raises(PermissionDeniedError):
            DecisionCaseService(conn=conn, principal=empty).append_version(
                "CASE-A-001", KIND_RAW_EVIDENCE, {"x": 1}
            )
    finally:
        conn.close()


def test_reads_require_analysis_read_capability(case_db):
    conn = _open(case_db)
    try:
        _service(conn).create_case("CASE-B-001", "讀取權測試")
        conn.commit()

        viewer = _principal(conn, "viewer")
        for operation in (
            lambda: _service(conn, "viewer").get_case("CASE-B-001"),
            lambda: _service(conn, "viewer").list_events("CASE-B-001"),
            lambda: _service(conn, "viewer").list_versions("CASE-B-001"),
            lambda: _service(conn, "viewer").list_runs("CASE-B-001"),
        ):
            with pytest.raises(PermissionDeniedError):
                operation()

        with pytest.raises(PermissionDeniedError):
            DecisionCaseService(conn=conn, principal=None).get_case("CASE-B-001")
    finally:
        conn.close()


def test_principal_resolved_from_isolated_db_without_streamlit(case_db):
    """既有契約的 load_principal 在隔離 SQLite 上即可解析；
    授權判定不依賴 Streamlit session（backend 套件的間接 import
    與 session 判斷無關）、不碰正式資料庫。"""
    conn = _open(case_db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users "
            "(username TEXT PRIMARY KEY, password TEXT, role TEXT, name TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_organizations "
            "(username TEXT PRIMARY KEY, organization_id TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS organization_entitlements "
            "(organization_id TEXT NOT NULL, entitlement_key TEXT NOT NULL, "
            "enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)), "
            "PRIMARY KEY (organization_id, entitlement_key))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO users VALUES ('planner', 'x', "
            "'supply_planner', '規劃員')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO user_organizations VALUES ('planner', 'org-syn-1')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO app_metadata VALUES "
            "('deployment_organization_id', 'org-syn-1')"
        )
        for entitlement in ("l1_monitor", "l2_decision", "l3_governed_action"):
            conn.execute(
                "INSERT OR REPLACE INTO organization_entitlements VALUES "
                "('org-syn-1', ?, 1)",
                (entitlement,),
            )
        conn.execute(
            "INSERT OR REPLACE INTO users VALUES ('viewer', 'x', 'risk_viewer', '觀測員')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO user_organizations VALUES ('viewer', 'org-syn-1')"
        )

        planner = load_principal("planner", conn=conn)
        assert planner is not None
        assert planner.can(RISK_WORKSPACE_WRITE)
        assert planner.can(RISK_ANALYSIS_READ)

        _service(conn, "planner").create_case("CASE-DB-001", "DB 解析 principal")
        assert _service(conn, "planner").get_case("CASE-DB-001") is not None

        viewer = load_principal("viewer", conn=conn)
        assert viewer is not None
        with pytest.raises(PermissionDeniedError):
            _service(conn, "viewer").get_case("CASE-DB-001")

        assert load_principal("no-such-user", conn=conn) is None
        with pytest.raises(PermissionDeniedError):
            DecisionCaseService(conn=conn, principal=None).get_case("CASE-DB-001")
        conn.commit()
    finally:
        conn.close()


# ── 驗收 6：合成 fixture 獨立完成、schema 不帶核准／proposal／receipt ──

def test_fixture_build_matches_fixture_exactly_and_no_approval_schema(case_db):
    fixture = _load_fixture()
    conn = _open(case_db)
    try:
        _build_case_from_fixture(conn, fixture)
        conn.commit()

        service = _service(conn)
        for column_set in (
            {row[1] for row in conn.execute("PRAGMA table_info(decision_cases)")},
            {row[1] for row in conn.execute("PRAGMA table_info(decision_case_events)")},
            {
                row[1]
                for row in conn.execute("PRAGMA table_info(decision_case_exposure_mappings)")
            },
            {row[1] for row in conn.execute("PRAGMA table_info(decision_case_versions)")},
            {
                row[1]
                for row in conn.execute("PRAGMA table_info(decision_case_scenario_runs)")
            },
        ):
            assert "approval_id" not in column_set
            assert "approval_status" not in column_set
            assert "operation_id" not in column_set

        # 隔離 DB 中除了本任務的表與既有授權表，沒有任何其他表
        # （證明未跑正式 init_db；正式 init_db 會建立更多業務表）
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND "
                "name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == {
            "decision_cases",
            "decision_case_events",
            "decision_case_versions",
            "decision_case_exposure_mappings",
            "decision_case_scenario_runs",
            # 既有授權契約的資料表（測試隔離資料）
            "users",
            "user_organizations",
            "organization_entitlements",
            "app_metadata",
        }
        assert service.list_versions(fixture["case_id"], KIND_SCENARIO_INPUT)[-1].version == 2
    finally:
        conn.close()


# ── 邊界：malformed JSON、重複 ID／版本、交易 rollback、未知 case、run 引用的版本 ──

def test_malformed_json_fails_closed_on_read(case_db):
    conn = _open(case_db)
    try:
        service = _service(conn)
        service.create_case("CASE-J-001", "JSON 損壞測試")
        service.append_version("CASE-J-001", KIND_RAW_EVIDENCE, {"ok": 1})
        service.append_version("CASE-J-001", KIND_SCENARIO_INPUT, {"ok": 1})
        service.record_scenario_run("CASE-J-001", "RUN-J-001", 1, {"score": 1})
        conn.commit()

        # 版本表是 append-only（UPDATE／DELETE 被本任務 trigger 拒絕），
        # 因此用「原始 INSERT 繞過服務驗證」模擬壞 JSON 寫入。
        conn.execute(
            "INSERT INTO decision_case_versions "
            "(case_id, kind, version, content_json, created_by, created_at) "
            "VALUES ('CASE-J-001', 'raw_evidence', 3, 'not-json{{', "
            "'planner', '2026-09-12T00:00:00Z')"
        )
        conn.commit()

        with pytest.raises(DecisionCaseDataError):
            service.list_versions("CASE-J-001", KIND_RAW_EVIDENCE)

        conn.execute(
            "UPDATE decision_case_scenario_runs SET result_json = 'oops{{' "
            "WHERE run_id = 'RUN-J-001'"
        )
        conn.commit()
        with pytest.raises(DecisionCaseDataError):
            service.list_runs("CASE-J-001")
    finally:
        conn.close()


def test_duplicate_ids_rejected(case_db):
    conn = _open(case_db)
    try:
        service = _service(conn)
        service.create_case("CASE-D-001", "重複測試")
        with pytest.raises(sqlite3.IntegrityError):
            service.create_case("CASE-D-001", "重複測試")
        service.add_mapping("MAP-D-1", "CASE-D-001", "supplier", "S1", STATUS_CONFIRMED, "")
        with pytest.raises(sqlite3.IntegrityError):
            service.add_mapping("MAP-D-1", "CASE-D-001", "supplier", "S2", STATUS_CANDIDATE, "")
        service.append_version("CASE-D-001", KIND_SCENARIO_INPUT, {"v": 1})
        service.record_scenario_run("CASE-D-001", "RUN-D-1", 1, {"score": 1})
        with pytest.raises(sqlite3.IntegrityError):
            service.record_scenario_run("CASE-D-001", "RUN-D-1", 1, {"score": 2})
        conn.commit()
    finally:
        conn.close()


def test_duplicate_version_rejected_via_repository(case_db):
    conn = _open(case_db)
    try:
        _service(conn).create_case("CASE-V-002", "重複版本測試")
        conn.commit()
        from backend.decision_case_repository import encode_json

        payload = encode_json({"a": 1})
        repo_insert_version(
            conn, "CASE-V-002", KIND_RAW_EVIDENCE, 1, payload, "planner", "2026-09-12T00:00:00Z"
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo_insert_version(
                conn, "CASE-V-002", KIND_RAW_EVIDENCE, 1, payload, "planner", "2026-09-12T00:00:01Z"
            )
    finally:
        conn.close()


def test_transaction_rollback_leaves_nothing(case_db):
    conn = sqlite3.connect(case_db, isolation_level=None)
    try:
        init_schema(conn)
        service = _service(conn)
        conn.execute("BEGIN")
        service.create_case("CASE-T-001", "rollback 測試")
        service.add_event(
            event_id="EV-T-001",
            case_id="CASE-T-001",
            event_type="port_disruption",
            description="先寫入一筆",
        )
        with pytest.raises(ValueError):
            service.append_version("CASE-T-001", "bogus_kind", {"x": 1})
        conn.rollback()
    finally:
        conn.close()

    conn2 = _open(case_db)
    try:
        with pytest.raises(CaseNotFoundError):
            _service(conn2).get_case("CASE-T-001")
    finally:
        conn2.close()

    conn3 = _open(case_db)
    try:
        # case 不存在時 list 讀取 fail closed（拋出，不給空清單冒充成功）
        with pytest.raises(CaseNotFoundError):
            _service(conn3).list_events("CASE-T-001")
    finally:
        conn3.close()


def test_unknown_case_reads_fail_closed(case_db):
    conn = _open(case_db)
    try:
        service = _service(conn)
        with pytest.raises(CaseNotFoundError):
            service.get_case("CASE-NOPE")
        with pytest.raises(CaseNotFoundError):
            service.list_events("CASE-NOPE")
        with pytest.raises(CaseNotFoundError):
            service.list_versions("CASE-NOPE")
        with pytest.raises(CaseNotFoundError):
            service.list_runs("CASE-NOPE")
        with pytest.raises(CaseNotFoundError):
            service.list_mappings("CASE-NOPE")
    finally:
        conn.close()


def test_run_requires_existing_scenario_input_version(case_db):
    conn = _open(case_db)
    try:
        service = _service(conn)
        service.create_case("CASE-R-001", "run 版本引用測試")
        with pytest.raises(DecisionCaseDataError):
            service.record_scenario_run("CASE-R-001", "RUN-R-001", 5, {"score": 1})
        # 內容也必須是可序列化 dict；空 dict 合法、None 不合法
        service.append_version("CASE-R-001", KIND_SCENARIO_INPUT, {"v": 1})
        service.record_scenario_run("CASE-R-001", "RUN-R-002", 1, {"score": 1})
        assert service.list_runs("CASE-R-001")[0].stale is False
        conn.commit()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 修正回合新增反例：逐組對應 reviewer verdict
# ══════════════════════════════════════════════════════════════════════════

def _service_raw(conn, principal, *, auth_conn=None):
    """用任意 principal 建構 service（跨部署測試用）。"""
    return DecisionCaseService(conn=conn, principal=principal, auth_conn=auth_conn)


# ── 修正 1：actor 冒名（P1）──

def test_actor_spoofing_rejected_on_all_writes(case_db):
    """五種寫入的 actor 只能是已驗證 principal.username；冒名一律拒絕。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-ACT-1", "actor 冒名測試")
        svc.append_version("CASE-ACT-1", KIND_SCENARIO_INPUT, {"v": 1})

        with pytest.raises(PermissionDeniedError):
            svc.create_case("CASE-ACT-2", "冒名建案", actor="admin")
        with pytest.raises(PermissionDeniedError):
            svc.add_event(
                "EV-ACT-1", "CASE-ACT-1", "port_disruption", actor="admin"
            )
        with pytest.raises(PermissionDeniedError):
            svc.add_mapping(
                "MAP-ACT-1", "CASE-ACT-1", "supplier", "S1",
                STATUS_CONFIRMED, "", actor="admin",
            )
        with pytest.raises(PermissionDeniedError):
            svc.append_version(
                "CASE-ACT-1", KIND_RAW_EVIDENCE, {"v": 2}, actor="admin"
            )
        with pytest.raises(PermissionDeniedError):
            svc.record_scenario_run(
                "CASE-ACT-1", "RUN-ACT-1", 1, {"s": 1}, actor="admin"
            )

        # 冒名失敗後不得留下任何 admin 痕跡
        assert conn.execute(
            "SELECT COUNT(*) FROM decision_cases WHERE created_by = 'admin'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM decision_case_events WHERE recorded_by = 'admin'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM decision_case_exposure_mappings "
            "WHERE created_by = 'admin'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM decision_case_versions WHERE created_by = 'admin'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM decision_case_scenario_runs WHERE created_by = 'admin'"
        ).fetchone()[0] == 0
        conn.commit()
    finally:
        conn.close()


def test_actor_binding_persists_verified_username(case_db):
    """actor 省略或與 principal 相同時，持久化的一定是已驗證 username。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-ACT-9", "身分記錄", actor="planner")
        svc.add_event(
            "EV-ACT-9", "CASE-ACT-9", "port_disruption", actor="planner"
        )
        svc.add_mapping(
            "MAP-ACT-9", "CASE-ACT-9", "supplier", "S9",
            STATUS_CONFIRMED, "", actor="planner",
        )
        svc.append_version("CASE-ACT-9", KIND_RAW_EVIDENCE, {"v": 1})  # actor=None
        for table, column in (
            ("decision_cases", "created_by"),
            ("decision_case_events", "recorded_by"),
            ("decision_case_exposure_mappings", "created_by"),
            ("decision_case_versions", "created_by"),
        ):
            rows = conn.execute(
                f"SELECT {column} FROM {table} WHERE case_id = 'CASE-ACT-9'"
            ).fetchall()
            assert rows and all(row[0] == "planner" for row in rows)
        conn.commit()
    finally:
        conn.close()


# ── 修正 2：既有 principal／部署邊界（P1）──

def test_auth_boundary_disabled_entitlement_denied(case_db):
    """建構後停用 entitlement：下次操作必須以授權連線重新解析並拒絕。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-AUTH-D", "失效 entitlement")
        conn.execute(
            "UPDATE organization_entitlements SET enabled = 0 "
            "WHERE organization_id = 'org-syn-1' AND entitlement_key = 'l2_decision'"
        )
        conn.commit()
        with pytest.raises(PermissionDeniedError):
            svc.get_case("CASE-AUTH-D")
        with pytest.raises(PermissionDeniedError):
            svc.create_case("CASE-AUTH-D2", "失效後寫入")
    finally:
        conn.close()


def test_auth_boundary_empty_username_denied(case_db):
    """空 username、卻帶 capabilities 的 AccessContext 一律拒絕。"""
    conn = _open(case_db)
    try:
        _service(conn).create_case("CASE-AUTH-E", "空身分")
        conn.commit()
        ghost = AccessContext(
            username="   ",
            role="admin",
            name="x",
            organization_id="org-syn-1",
            entitlements=frozenset(),
            capabilities=frozenset({RISK_ANALYSIS_READ, RISK_WORKSPACE_WRITE}),
        )
        svc = DecisionCaseService(conn=conn, principal=ghost)
        with pytest.raises(PermissionDeniedError):
            svc.get_case("CASE-AUTH-E")
        with pytest.raises(PermissionDeniedError):
            svc.create_case("CASE-AUTH-E2", "空身分寫入")
    finally:
        conn.close()


def test_auth_boundary_cross_deployment_denied(tmp_path):
    """org-A 解析的 principal 不得用於 org-B 的案件 store（部署綁定）。"""
    store_path = tmp_path / "store.db"
    auth_path = tmp_path / "auth.db"
    store_setup = sqlite3.connect(store_path)
    init_schema(store_setup)
    _seed_auth_tables(store_setup, org="org-B")
    store_setup.commit()
    store_setup.close()

    auth = sqlite3.connect(auth_path)
    _seed_auth_tables(auth, org="org-A")
    auth.commit()

    alice = load_principal("planner", conn=auth)
    assert alice is not None and alice.organization_id == "org-A"

    store = sqlite3.connect(store_path)
    init_schema(store)
    _service(store, "planner").create_case("CASE-XORG-1", "跨部署案件")
    store.commit()

    svc = _service_raw(store, alice, auth_conn=auth)
    with pytest.raises(PermissionDeniedError):
        svc.get_case("CASE-XORG-1")
    with pytest.raises(PermissionDeniedError):
        svc.create_case("CASE-XORG-2", "越部署寫入")
    store.close()
    auth.close()


# ── 修正 3：init_schema 外鍵（P1）──

def test_fk_init_schema_refuses_inside_active_transaction():
    """active transaction 內 PRAGMA foreign_keys 無效時明確拒絕、不得偷 commit。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("BEGIN")
    with pytest.raises(DecisionCaseDataError):
        init_schema(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    assert conn.in_transaction, "拒絕時不得偷偷 commit 呼叫端交易"
    conn.rollback()

    init_schema(conn)  # 交易結束後重試成功
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_fk_enforced_after_init_and_missing_case_writes_fail_closed(case_db):
    """初始化後 foreign_keys=1；service 對不存在 case 的寫入一律 fail closed。"""
    conn = _open(case_db)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        svc = _service(conn)
        with pytest.raises(CaseNotFoundError):
            svc.add_event("EV-NOCASE", "NO-CASE", "port_disruption")
        with pytest.raises(CaseNotFoundError):
            svc.add_mapping(
                "MAP-NOCASE", "NO-CASE", "supplier", "S", STATUS_CONFIRMED, ""
            )
        with pytest.raises(CaseNotFoundError):
            svc.append_version("NO-CASE", KIND_RAW_EVIDENCE, {"v": 1})
        with pytest.raises(CaseNotFoundError):
            svc.record_scenario_run("NO-CASE", "RUN-NOCASE", 1, {"s": 1})
    finally:
        conn.close()


# ── 修正 4：run 引用與 stale（P1）──

def test_run_ref_version_delete_blocked_and_stale_not_resurrected(case_db):
    """版本不可刪退：DELETE 被拒，舊 run 保持永久 stale。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-S-002", "stale 不可逆")
        svc.append_version("CASE-S-002", KIND_SCENARIO_INPUT, {"v": 1})
        svc.record_scenario_run("CASE-S-002", "RUN-1", 1, {"s": 1})
        svc.append_version("CASE-S-002", KIND_SCENARIO_INPUT, {"v": 2})
        assert svc.list_runs("CASE-S-002")[0].stale is True
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM decision_case_versions "
                "WHERE case_id = 'CASE-S-002' AND version = 2"
            )
        assert svc.list_runs("CASE-S-002")[0].stale is True, (
            "刪退被拒後，stale 不可逆性質必須維持"
        )
    finally:
        conn.close()


def test_run_ref_corrupt_input_content_rejected(case_db):
    """引用已存在但內容損壞的 scenario_input 必須拒絕（不只看存在與否）。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-C-002", "損壞輸入")
        svc.append_version("CASE-C-002", KIND_SCENARIO_INPUT, {"v": 1})
        svc.append_version("CASE-C-002", KIND_SCENARIO_INPUT, {"v": 2})
        conn.commit()
        # 原始 INSERT 繞過服務驗證製造損壞輸入（UPDATE 已被 append-only trigger 擋）
        conn.execute(
            "INSERT INTO decision_case_versions "
            "(case_id, kind, version, content_json, created_by, created_at) "
            "VALUES ('CASE-C-002', 'scenario_input', 3, 'broken{{', "
            "'planner', '2026-09-12T00:00:00Z')"
        )
        conn.commit()
        with pytest.raises(DecisionCaseDataError):
            svc.record_scenario_run("CASE-C-002", "RUN-C-9", 3, {"s": 1})
    finally:
        conn.close()


def test_run_ref_read_rejects_corrupt_referenced_input(case_db):
    """讀取引用損壞輸入的既有 run 必須 fail closed，不得回 stale=False。"""
    raw = sqlite3.connect(case_db)
    raw.execute("PRAGMA foreign_keys = OFF")
    raw.execute(
        "INSERT INTO decision_cases VALUES "
        "('CASE-C-003', 'x', 'open', 'planner', '2026-09-12T00:00:00Z')"
    )
    raw.execute(
        "INSERT INTO decision_case_versions VALUES "
        "('CASE-C-003', 'scenario_input', 1, 'broken{{', "
        "'planner', '2026-09-12T00:00:00Z')"
    )
    raw.execute(
        "INSERT INTO decision_case_scenario_runs VALUES "
        "('RUN-C-3', 'CASE-C-003', 1, '{\"s\":1}', "
        "'planner', '2026-09-12T00:00:00Z')"
    )
    raw.commit()
    raw.close()

    conn = _open(case_db)
    try:
        with pytest.raises(DecisionCaseDataError):
            _service(conn).get_run("RUN-C-3")
    finally:
        conn.close()


def test_orphan_mapping_and_run_get_fail_closed(case_db):
    """失去父案件的 mapping／run 不得回正常物件。"""
    raw = sqlite3.connect(case_db)
    raw.execute("PRAGMA foreign_keys = OFF")
    raw.execute(
        "INSERT INTO decision_cases VALUES "
        "('GHOST-CASE', 'x', 'open', 'x', '2026-09-12T00:00:00Z')"
    )
    raw.execute(
        "INSERT INTO decision_case_exposure_mappings VALUES "
        "('MAP-GHOST', 'GHOST-CASE', 'supplier', 'S', 'confirmed', '', "
        "'x', '2026-09-12T00:00:00Z')"
    )
    raw.execute(
        "INSERT INTO decision_case_versions VALUES "
        "('GHOST-CASE', 'scenario_input', 1, '{\"v\":1}', "
        "'x', '2026-09-12T00:00:00Z')"
    )
    raw.execute(
        "INSERT INTO decision_case_scenario_runs VALUES "
        "('RUN-GHOST', 'GHOST-CASE', 1, '{\"s\":1}', 'x', '2026-09-12T00:00:00Z')"
    )
    raw.commit()
    raw.execute("DELETE FROM decision_cases WHERE case_id = 'GHOST-CASE'")
    raw.commit()
    raw.close()

    conn = _open(case_db)
    try:
        svc = _service(conn)
        with pytest.raises(DecisionCaseDataError):
            svc.get_mapping("MAP-GHOST")
        with pytest.raises(DecisionCaseDataError):
            svc.get_run("RUN-GHOST")
    finally:
        conn.close()


# ── 修正 5：矛盾 mapping（P1）──

def test_mapping_conflicting_status_rejected(case_db):
    """同一 (case, subject_type, subject_key) 不得存在第二筆矛盾 mapping。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-M-002", "矛盾 mapping")
        svc.add_mapping(
            "MAP-CONF-1", "CASE-M-002", "supplier", "S",
            STATUS_CONFIRMED, "",
        )
        with pytest.raises(sqlite3.IntegrityError):
            svc.add_mapping(
                "MAP-CONF-2", "CASE-M-002", "supplier", "S",
                STATUS_UNKNOWN, "",
            )
        # 查詢結果唯一且確定
        assert svc.mapping_status_for("CASE-M-002", "supplier", "S") == STATUS_CONFIRMED
        found = svc.find_mapping("CASE-M-002", "supplier", "S")
        assert found is not None and found.mapping_id == "MAP-CONF-1"
        conn.commit()
    finally:
        conn.close()


def test_mapping_subject_normalization_read_write_consistent(case_db):
    """寫入與查詢採用同一 subject 正規化（strip）。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-M-003", "正規化")
        svc.add_mapping(
            "MAP-NORM-1", "CASE-M-003", " supplier ", " SPACED ",
            STATUS_CONFIRMED, "",
        )
        # 查詢側（原字串或已正規化字串）都必須命中同一筆
        assert (
            svc.mapping_status_for("CASE-M-003", " supplier ", " SPACED ")
            == STATUS_CONFIRMED
        )
        assert svc.find_mapping("CASE-M-003", "supplier", "SPACED") is not None
        conn.commit()
    finally:
        conn.close()


# ── 修正 6：版本競爭（P2）──

def test_concurrent_appends_get_distinct_versions(tmp_path, monkeypatch):
    """兩個 connection 合法競爭追加：不得撞號、不得靜默遺失。"""
    db_path = tmp_path / "race.db"
    setup = sqlite3.connect(db_path)
    init_schema(setup)
    _seed_auth_tables(setup)
    _service(setup).create_case("CASE-RACE-1", "競爭測試")
    setup.commit()
    setup.close()

    import backend.decision_cases as svc_mod

    barrier = threading.Barrier(2)
    real_max_version = svc_mod.repo.max_version

    def racing_max(conn, case_id, kind):
        value = real_max_version(conn, case_id, kind)
        # 兩執行緒都算完 MAX 後才放行，重現舊實作的 SELECT→INSERT 時窗
        barrier.wait(timeout=10)
        return value

    monkeypatch.setattr(svc_mod.repo, "max_version", racing_max)

    results = {}

    def worker(name, payload):
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout = 10000")
        init_schema(conn)
        try:
            version = _service(conn).append_version(
                "CASE-RACE-1", KIND_RAW_EVIDENCE, payload
            )
            conn.commit()  # transaction ownership 屬於呼叫端
            results[name] = ("OK", version.version, version.content)
        except Exception as exc:  # noqa: BLE001 — 競爭結果需原樣記錄
            results[name] = (type(exc).__name__, str(exc))
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=(name, {"worker": name}))
        for name in ("A", "B")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    monkeypatch.setattr(svc_mod.repo, "max_version", real_max_version)

    ok = {name: res for name, res in results.items() if res[0] == "OK"}
    assert set(ok) == {"A", "B"}, f"合法競爭不得靜默失敗：{results}"
    assert {res[1] for res in ok.values()} == {1, 2}, (
        f"兩次追加必須拿到不同版本號：{ok}"
    )

    verify = sqlite3.connect(db_path)
    init_schema(verify)
    rows = verify.execute(
        "SELECT version, content_json FROM decision_case_versions "
        "WHERE case_id = 'CASE-RACE-1' AND kind = 'raw_evidence' ORDER BY version"
    ).fetchall()
    assert len(rows) == 2
    workers = {json.loads(content)["worker"] for _, content in rows}
    assert workers == {"A", "B"}, "兩個 worker 的內容都必須保存，不得覆寫"
    verify.close()


# ── 修正 7：JSON 快照與有限值（P2）──

def test_json_snapshot_isolated_and_canonical(case_db):
    """回傳內容與 DB 內容出自同一 canonical JSON；呼叫端巢狀變動不影響快照。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-J-002", "JSON 快照")
        x = {"nested": {"values": [1]}}
        version = svc.append_version("CASE-J-002", KIND_RAW_EVIDENCE, x)
        x["nested"]["values"].append(99)  # 呼叫端巢狀變動
        assert version.content == {"nested": {"values": [1]}}

        stored = svc.list_versions("CASE-J-002", KIND_RAW_EVIDENCE)[0].content
        assert stored == {"nested": {"values": [1]}}
        assert version.content == stored, "回傳物件與 DB 內容必須一致"

        # tuple／int-key 統一正規化為 JSON canonical 形式
        version2 = svc.append_version(
            "CASE-J-002", KIND_MANUAL_CORRECTION, {1: (2, 3)}
        )
        assert version2.content == {"1": [2, 3]}
        stored2 = svc.list_versions(
            "CASE-J-002", KIND_MANUAL_CORRECTION
        )[0].content
        assert stored2 == {"1": [2, 3]}
        assert version2.content == stored2

        # run 結果同樣是快照
        svc.append_version("CASE-J-002", KIND_SCENARIO_INPUT, {"v": 1})
        result = {"m": {"n": [7]}}
        run = svc.record_scenario_run("CASE-J-002", "RUN-J-2", 1, result)
        result["m"]["n"].append(8)
        assert run.result == {"m": {"n": [7]}}
        conn.commit()
    finally:
        conn.close()


def test_json_non_finite_values_rejected(case_db):
    """NaN／Infinity／-Infinity 與溢位（1e999）在寫入與讀取都必須被拒。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-J-003", "有限值")
        for bad in (
            {"score": float("nan")},
            {"score": float("inf")},
            {"score": float("-inf")},
        ):
            with pytest.raises(DecisionCaseDataError):
                svc.append_version("CASE-J-003", KIND_RAW_EVIDENCE, bad)

        svc.append_version("CASE-J-003", KIND_SCENARIO_INPUT, {"v": 1})
        for bad in ({"score": float("nan")}, {"score": float("inf")}):
            with pytest.raises(DecisionCaseDataError):
                svc.record_scenario_run("CASE-J-003", "RUN-BAD-N", 1, bad)

        svc.append_version("CASE-J-003", KIND_RAW_EVIDENCE, {"v": 1})
        # 原始寫入的非有限值在讀取端也必須 fail closed
        conn.execute(
            "INSERT INTO decision_case_versions "
            "(case_id, kind, version, content_json, created_by, created_at) "
            "VALUES ('CASE-J-003', 'raw_evidence', 2, '{\"score\": NaN}', "
            "'planner', '2026-09-12T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO decision_case_versions "
            "(case_id, kind, version, content_json, created_by, created_at) "
            "VALUES ('CASE-J-003', 'raw_evidence', 3, '{\"score\": 1e999}', "
            "'planner', '2026-09-12T00:00:00Z')"
        )
        conn.commit()
        with pytest.raises(DecisionCaseDataError):
            svc.list_versions("CASE-J-003", KIND_RAW_EVIDENCE)
    finally:
        conn.close()


# ── 修正 8：append-only 的 DB 層保證（P2）──

def test_versions_update_and_delete_blocked_by_trigger(case_db):
    """decision_case_versions 的 UPDATE／DELETE 由本任務 trigger 直接拒絕。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-AO-1", "append-only DB 層")
        svc.append_version("CASE-AO-1", KIND_RAW_EVIDENCE, {"v": 1})
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE decision_case_versions SET content_json = '{\"v\":2}' "
                "WHERE case_id = 'CASE-AO-1'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM decision_case_versions WHERE case_id = 'CASE-AO-1'"
            )
        assert svc.list_versions("CASE-AO-1", KIND_RAW_EVIDENCE)[0].content == {"v": 1}
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 續修回合（GPT-6 CHANGES_REQUIRED）新增反例
# ══════════════════════════════════════════════════════════════════════════

# ── P1-a：跨部署同名 principal 不得被重新綁定（預設 auth_conn 路徑）──

def test_cross_deployment_same_name_default_path_read_denied(tmp_path):
    """省略 auth_conn 時，他部署同名 planner 不得讀取本部署案件（fail closed）。"""
    store_path = tmp_path / "store-b.db"
    auth_path = tmp_path / "auth-a.db"
    store_setup = sqlite3.connect(store_path)
    init_schema(store_setup)
    _seed_auth_tables(store_setup, org="org-B")
    _service(store_setup).create_case("B-CASE", "org-B 私有案件")
    store_setup.commit()
    store_setup.close()

    auth = sqlite3.connect(auth_path)
    _seed_auth_tables(auth, org="org-A")
    auth.commit()

    p_a = load_principal("planner", conn=auth)
    assert p_a is not None and p_a.organization_id == "org-A"

    store = _open(store_path)
    try:
        # 預設 auth_conn（同 conn）不得把 org-A 身分靜默換成 org-B
        bad = DecisionCaseService(conn=store, principal=p_a)
        with pytest.raises(PermissionDeniedError):
            bad.get_case("B-CASE")
        with pytest.raises(PermissionDeniedError):
            bad.list_events("B-CASE")
        assert bad._verified_principal is None, "拒絕後不得完成任何身分重綁"

        # 明確同連線（auth_conn == conn）同樣 fail closed
        bad_same = DecisionCaseService(conn=store, principal=p_a, auth_conn=store)
        with pytest.raises(PermissionDeniedError):
            bad_same.get_case("B-CASE")

        # 對照組：org-B 自身 planner 正常讀取
        ok = _service(store)
        assert ok.get_case("B-CASE").case_id == "B-CASE"
    finally:
        store.close()
        auth.close()


def test_cross_deployment_same_name_default_path_write_denied(tmp_path):
    """省略 auth_conn 時，他部署同名 planner 不得寫入本部署案件；同 org 正常。"""
    store_path = tmp_path / "store-b.db"
    auth_path = tmp_path / "auth-a.db"
    store_setup = sqlite3.connect(store_path)
    init_schema(store_setup)
    _seed_auth_tables(store_setup, org="org-B")
    _service(store_setup).create_case("B-CASE", "org-B 私有案件")
    store_setup.commit()
    store_setup.close()

    auth = sqlite3.connect(auth_path)
    _seed_auth_tables(auth, org="org-A")
    auth.commit()
    p_a = load_principal("planner", conn=auth)
    assert p_a is not None and p_a.organization_id == "org-A"

    store = _open(store_path)
    try:
        bad = DecisionCaseService(conn=store, principal=p_a)
        with pytest.raises(PermissionDeniedError):
            bad.add_event("CROSS-E", "B-CASE", "synthetic")
        with pytest.raises(PermissionDeniedError):
            bad.create_case("B-CASE-2", "越部署建案")
        with pytest.raises(PermissionDeniedError):
            bad.append_version("B-CASE", KIND_RAW_EVIDENCE, {"x": 1})

        # 被拒後不得在 org-B 留下任何痕跡
        assert store.execute(
            "SELECT COUNT(*) FROM decision_case_events WHERE event_id = 'CROSS-E'"
        ).fetchone()[0] == 0
        assert store.execute(
            "SELECT COUNT(*) FROM decision_cases WHERE case_id = 'B-CASE-2'"
        ).fetchone()[0] == 0
        assert store.execute(
            "SELECT COUNT(*) FROM decision_case_versions WHERE case_id = 'B-CASE'"
        ).fetchone()[0] == 0

        # 對照組：org-B 自身 planner 正常寫入
        ok = _service(store)
        ok.add_event("OK-E", "B-CASE", "synthetic")
        assert store.execute(
            "SELECT COUNT(*) FROM decision_case_events WHERE event_id = 'OK-E'"
        ).fetchone()[0] == 1
    finally:
        store.close()
        auth.close()


# ── P1-b：INSERT OR REPLACE 不得繞過 append-only trigger ──

def test_replace_same_composite_key_blocked_and_content_intact(case_db):
    """REPLACE 同 (case_id, kind, version) 被拒；原內容與 run 狀態不變；重連後仍被拒。"""
    conn = _open(case_db)
    try:
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
        svc = _service(conn)
        svc.create_case("CASE-REP-1", "REPLACE 覆寫測試")
        svc.append_version("CASE-REP-1", KIND_RAW_EVIDENCE, {"evidence": "original"})
        svc.append_version("CASE-REP-1", KIND_SCENARIO_INPUT, {"delay_days": 7})
        svc.record_scenario_run("CASE-REP-1", "RUN-REP-1", 1, {"score": 42})
        assert svc.list_runs("CASE-REP-1")[0].stale is False
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO decision_case_versions "
                "(case_id, kind, version, content_json, created_by, created_at) "
                "VALUES ('CASE-REP-1', 'raw_evidence', 1, "
                "'{\"replacement\": 99}', 'planner', '2026-09-13T00:00:00Z')"
            )

        raw = svc.list_versions("CASE-REP-1", KIND_RAW_EVIDENCE)
        assert [(v.version, v.content) for v in raw] == [(1, {"evidence": "original"})]
        inputs = svc.list_versions("CASE-REP-1", KIND_SCENARIO_INPUT)
        assert [(v.version, v.content) for v in inputs] == [(1, {"delay_days": 7})]
        assert svc.list_runs("CASE-REP-1")[0].stale is False
        conn.commit()
    finally:
        conn.close()

    # 關閉重連後：原內容仍在，REPLACE 仍被拒
    conn2 = _open(case_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn2.execute(
                "INSERT OR REPLACE INTO decision_case_versions "
                "(case_id, kind, version, content_json, created_by, created_at) "
                "VALUES ('CASE-REP-1', 'scenario_input', 1, "
                "'{\"delay_days\": 99}', 'planner', '2026-09-13T00:00:00Z')"
            )
        svc2 = _service(conn2)
        assert svc2.list_versions(
            "CASE-REP-1", KIND_SCENARIO_INPUT
        )[0].content == {"delay_days": 7}
        assert svc2.list_runs("CASE-REP-1")[0].stale is False
    finally:
        conn2.close()


def test_replace_rowid_conflict_blocked_and_stale_stays_true(case_db):
    """REPLACE 以 rowid 衝突刪退最新 scenario_input 被拒；舊 run 永遠維持 stale。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("C", "rowid REPLACE 測試")
        svc.append_version("C", KIND_SCENARIO_INPUT, {"v": 1})
        svc.record_scenario_run("C", "OLD", 1, {"score": 10})
        svc.append_version("C", KIND_SCENARIO_INPUT, {"v": 2})
        assert svc.list_runs("C")[0].stale is True
        conn.commit()

        (rowid_v2,) = conn.execute(
            "SELECT rowid FROM decision_case_versions "
            "WHERE case_id = 'C' AND kind = 'scenario_input' AND version = 2"
        ).fetchone()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO decision_case_versions "
                "(rowid, case_id, kind, version, content_json, created_by, created_at) "
                "VALUES (?, 'C', 'raw_evidence', 1, "
                "'{\"evidence\":\"synthetic\"}', 'planner', 'synthetic')",
                (rowid_v2,),
            )

        assert svc.list_runs("C")[0].stale is True, (
            "rowid REPLACE 被拒後，舊 run 不得由 True 回復 False"
        )
        remaining = conn.execute(
            "SELECT version, content_json FROM decision_case_versions "
            "WHERE case_id = 'C' AND kind = 'scenario_input' ORDER BY version"
        ).fetchall()
        assert [row[0] for row in remaining] == [1, 2]
        assert json.loads(remaining[1][1]) == {"v": 2}
        conn.commit()
    finally:
        conn.close()

    # 關閉重連後：stale 仍為 True、版本仍為 1..2
    conn2 = _open(case_db)
    try:
        svc2 = _service(conn2)
        assert svc2.list_runs("C")[0].stale is True
        assert [
            v.version for v in svc2.list_versions("C", KIND_SCENARIO_INPUT)
        ] == [1, 2]
    finally:
        conn2.close()


def test_init_schema_enables_recursive_triggers_in_active_transaction():
    """fk 已開啟的 active transaction 內 init_schema 可初始化、不偷 commit、rt=ON。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN")
    init_schema(conn)
    assert conn.in_transaction, "init_schema 不得 commit 呼叫端交易"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'decision_case_%'"
        )
    }
    assert "decision_case_versions" in tables
    conn.rollback()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 唯一修正回合：REPLACE 保護不得依賴 recursive_triggers（parent verdict）
# 硬要求 1：init_schema 後 PRAGMA recursive_triggers=OFF，同 PK REPLACE 仍拒絕。
# 硬要求 2：同情況 rowid 衝突 REPLACE 仍拒絕。
# 硬要求 3：關閉重連、不再次 init_schema（驗證 DB schema 自身），兩種 REPLACE 仍拒絕。
# 硬要求 4：原內容與版本完整不變，舊 run stale 永遠不復活。
# 硬要求 5：正常 append 與雙連線競爭追加不退化（競爭由既有 race 測試守住）。
# ══════════════════════════════════════════════════════════════════════════

def test_replace_same_pk_blocked_with_recursive_triggers_off(case_db):
    """rt=OFF（同連線）：同 (case_id, kind, version) 的 REPLACE 仍拒絕，
    原內容與版本不變、舊 run 永不復活。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-RT0-1", "rt=OFF 同 PK REPLACE")
        svc.append_version("CASE-RT0-1", KIND_RAW_EVIDENCE, {"evidence": "original"})
        svc.append_version("CASE-RT0-1", KIND_SCENARIO_INPUT, {"delay_days": 7})
        svc.record_scenario_run("CASE-RT0-1", "RUN-RT0-1", 1, {"score": 42})
        svc.append_version("CASE-RT0-1", KIND_SCENARIO_INPUT, {"delay_days": 14})
        assert svc.list_runs("CASE-RT0-1")[0].stale is True
        conn.commit()

        conn.execute("PRAGMA recursive_triggers = OFF")
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO decision_case_versions "
                "(case_id, kind, version, content_json, created_by, created_at) "
                "VALUES ('CASE-RT0-1', 'raw_evidence', 1, "
                "'{\"replacement\": 99}', 'planner', '2026-09-13T00:00:00Z')"
            )

        raw = svc.list_versions("CASE-RT0-1", KIND_RAW_EVIDENCE)
        assert [(v.version, v.content) for v in raw] == [(1, {"evidence": "original"})]
        inputs = svc.list_versions("CASE-RT0-1", KIND_SCENARIO_INPUT)
        assert [(v.version, v.content) for v in inputs] == [
            (1, {"delay_days": 7}),
            (2, {"delay_days": 14}),
        ]
        assert svc.list_runs("CASE-RT0-1")[0].stale is True, "舊 run 不得復活"
        conn.commit()
    finally:
        conn.close()


def test_replace_rowid_conflict_blocked_with_recursive_triggers_off(case_db):
    """rt=OFF（同連線）：明確 NEW.rowid 撞既有列（刪退最新 scenario_input）的
    REPLACE 仍拒絕；版本 1..2 完整，舊 run stale 永遠維持。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-RT0-2", "rt=OFF rowid REPLACE")
        svc.append_version("CASE-RT0-2", KIND_SCENARIO_INPUT, {"v": 1})
        svc.record_scenario_run("CASE-RT0-2", "OLD", 1, {"score": 10})
        svc.append_version("CASE-RT0-2", KIND_SCENARIO_INPUT, {"v": 2})
        assert svc.list_runs("CASE-RT0-2")[0].stale is True
        conn.commit()

        conn.execute("PRAGMA recursive_triggers = OFF")
        (rowid_v2,) = conn.execute(
            "SELECT rowid FROM decision_case_versions "
            "WHERE case_id = 'CASE-RT0-2' AND kind = 'scenario_input' AND version = 2"
        ).fetchone()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO decision_case_versions "
                "(rowid, case_id, kind, version, content_json, created_by, created_at) "
                "VALUES (?, 'CASE-RT0-2', 'raw_evidence', 1, "
                "'{\"evidence\":\"synthetic\"}', 'planner', 'synthetic')",
                (rowid_v2,),
            )

        assert svc.list_runs("CASE-RT0-2")[0].stale is True, (
            "rowid REPLACE 被拒後，舊 run 不得由 True 回復 False"
        )
        remaining = conn.execute(
            "SELECT version, content_json FROM decision_case_versions "
            "WHERE case_id = 'CASE-RT0-2' AND kind = 'scenario_input' ORDER BY version"
        ).fetchall()
        assert [row[0] for row in remaining] == [1, 2]
        assert json.loads(remaining[1][1]) == {"v": 2}
        conn.commit()
    finally:
        conn.close()


def test_replace_blocked_on_reconnect_without_init_schema(case_db):
    """關閉重連、不再次 init_schema：保護來自 DB schema 自身（trigger 持久於
    sqlite_master），兩種 REPLACE 在新連線（rt 預設 0）仍拒絕、內容不變。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-NOINIT-1", "重連不 init")
        svc.append_version("CASE-NOINIT-1", KIND_RAW_EVIDENCE, {"evidence": "original"})
        svc.append_version("CASE-NOINIT-1", KIND_SCENARIO_INPUT, {"v": 1})
        svc.record_scenario_run("CASE-NOINIT-1", "RUN-NI-1", 1, {"score": 1})
        svc.append_version("CASE-NOINIT-1", KIND_SCENARIO_INPUT, {"v": 2})
        conn.commit()
    finally:
        conn.close()

    # 新連線：不呼叫 init_schema（recursive_triggers／foreign_keys 皆為預設 0）
    raw = sqlite3.connect(case_db)
    try:
        assert raw.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT OR REPLACE INTO decision_case_versions "
                "(case_id, kind, version, content_json, created_by, created_at) "
                "VALUES ('CASE-NOINIT-1', 'raw_evidence', 1, "
                "'{\"replacement\": 99}', 'planner', '2026-09-13T00:00:00Z')"
            )
        (rowid_v2,) = raw.execute(
            "SELECT rowid FROM decision_case_versions "
            "WHERE case_id = 'CASE-NOINIT-1' AND kind = 'scenario_input' AND version = 2"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT OR REPLACE INTO decision_case_versions "
                "(rowid, case_id, kind, version, content_json, created_by, created_at) "
                "VALUES (?, 'CASE-NOINIT-1', 'raw_evidence', 9, "
                "'{\"evidence\":\"synthetic\"}', 'planner', 'synthetic')",
                (rowid_v2,),
            )
        rows = raw.execute(
            "SELECT kind, version, content_json FROM decision_case_versions "
            "WHERE case_id = 'CASE-NOINIT-1' ORDER BY kind, version"
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [
            ("raw_evidence", 1),
            ("scenario_input", 1),
            ("scenario_input", 2),
        ]
        assert json.loads(rows[0][2]) == {"evidence": "original"}
    finally:
        raw.close()

    conn2 = _open(case_db)
    try:
        assert _service(conn2).list_runs("CASE-NOINIT-1")[0].stale is True, (
            "重連後舊 run stale 必須維持，不得復活"
        )
    finally:
        conn2.close()


def test_normal_append_and_no_conflict_replace_unaffected(case_db):
    """硬要求 5：正常 append（含 rt=OFF 連線）不退化；無衝突的 REPLACE
    （全新 PK）等同純 append，不得被誤擋。"""
    conn = _open(case_db)
    try:
        svc = _service(conn)
        svc.create_case("CASE-APP-1", "append 不退化")
        v1 = svc.append_version("CASE-APP-1", KIND_RAW_EVIDENCE, {"v": 1}).version
        conn.execute("PRAGMA recursive_triggers = OFF")
        v2 = svc.append_version("CASE-APP-1", KIND_RAW_EVIDENCE, {"v": 2}).version
        v3 = svc.append_version("CASE-APP-1", KIND_SCENARIO_INPUT, {"v": 1}).version
        assert (v1, v2, v3) == (1, 2, 1)
        conn.execute(
            "INSERT OR REPLACE INTO decision_case_versions "
            "(case_id, kind, version, content_json, created_by, created_at) "
            "VALUES ('CASE-APP-1', 'raw_evidence', 3, '{\"v\":3}', 'planner', 't')"
        )
        assert [
            v.version for v in svc.list_versions("CASE-APP-1", KIND_RAW_EVIDENCE)
        ] == [1, 2, 3]
        conn.commit()
    finally:
        conn.close()
