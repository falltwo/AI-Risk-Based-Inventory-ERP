"""SQLite repository for decision cases.

Isolation contract (auditable):
- Every function takes an explicit ``sqlite3.Connection``; this module never
  opens its own connection, never imports ``backend.database``, never touches
  the global ``DB_FILE`` and never runs any schema change other than
  ``init_schema(conn)`` on the connection the caller passes in.
- All tables live in their own ``decision_case_*`` namespace and are created
  with ``CREATE TABLE IF NOT EXISTS`` only.
- Version rows are append-only.  This is enforced by BEFORE UPDATE/DELETE
  triggers plus a BEFORE INSERT trigger that rejects ``INSERT OR REPLACE``
  conflicts — an existing row with the same (case_id, kind, version), or an
  explicit NEW.rowid that collides with an existing row — *before* the
  replace's implicit delete, so the guarantee holds on any connection,
  including ones with ``recursive_triggers = OFF`` or reopened without
  re-running ``init_schema``.  ``PRAGMA recursive_triggers = ON`` is kept
  as defense-in-depth, not as the sole protection.  This is a
  database-level guarantee *within these task tables* — it does not protect
  against a DBA dropping the triggers or rewriting the schema.  Version
  numbers are assigned by one atomic
  ``INSERT ... SELECT COALESCE(MAX(version), 0) + 1 ... RETURNING``
  statement, never by a separate SELECT-MAX-then-INSERT pair.
- scenario run references are protected by a BEFORE INSERT trigger that
  requires the referenced ``scenario_input`` version to exist.
- Malformed JSON — including non-finite numbers (NaN/Infinity/overflow) —
  anywhere fails closed with :class:`DecisionCaseDataError`.
"""

from __future__ import annotations

import json
import math
import sqlite3

KIND_RAW_EVIDENCE = "raw_evidence"
KIND_MANUAL_CORRECTION = "manual_correction"
KIND_SCENARIO_INPUT = "scenario_input"
VALID_VERSION_KINDS = frozenset(
    {KIND_RAW_EVIDENCE, KIND_MANUAL_CORRECTION, KIND_SCENARIO_INPUT}
)

STATUS_CONFIRMED = "confirmed"
STATUS_CANDIDATE = "candidate"
STATUS_UNKNOWN = "unknown"
STATUS_UNMAPPED = "unmapped"
VALID_MAPPING_STATUSES = frozenset(
    {STATUS_CONFIRMED, STATUS_CANDIDATE, STATUS_UNKNOWN, STATUS_UNMAPPED}
)

# 未命中（miss）永遠停在 unmapped／unknown，絕不自動升格為正式曝險，
# 也絕不放進「安全」這類 status —— 這是 fail-closed 的一部分。
_CASE_STATUS_OPEN = "open"


class DecisionCaseDataError(ValueError):
    """決策案例資料損壞或違反版本語意時拋出（fail closed）。"""


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS decision_cases (
        case_id    TEXT PRIMARY KEY,
        title      TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'open',
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_case_events (
        event_id     TEXT PRIMARY KEY,
        case_id      TEXT NOT NULL,
        event_type   TEXT NOT NULL,
        region       TEXT NOT NULL DEFAULT '',
        country      TEXT NOT NULL DEFAULT '',
        description  TEXT NOT NULL DEFAULT '',
        occurred_at  TEXT NOT NULL DEFAULT '',
        recorded_by  TEXT NOT NULL,
        recorded_at  TEXT NOT NULL,
        FOREIGN KEY (case_id) REFERENCES decision_cases(case_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_case_exposure_mappings (
        mapping_id   TEXT PRIMARY KEY,
        case_id      TEXT NOT NULL,
        subject_type TEXT NOT NULL,
        subject_key  TEXT NOT NULL,
        status       TEXT NOT NULL
                     CHECK (status IN ('confirmed', 'candidate', 'unknown', 'unmapped')),
        rationale    TEXT NOT NULL DEFAULT '',
        created_by   TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        FOREIGN KEY (case_id) REFERENCES decision_cases(case_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_decision_case_mappings_case
        ON decision_case_exposure_mappings(case_id)
    """,
    # 同一 (case_id, subject_type, subject_key) 只允許一筆 mapping：
    # 重複／矛盾狀態在 DB 語意上直接被拒絕，不是只靠查詢排序隱藏。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_decision_case_mappings_subject
        ON decision_case_exposure_mappings(case_id, subject_type, subject_key)
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_case_versions (
        case_id      TEXT NOT NULL,
        kind         TEXT NOT NULL
                     CHECK (kind IN ('raw_evidence', 'manual_correction', 'scenario_input')),
        version      INTEGER NOT NULL CHECK (version > 0),
        content_json TEXT NOT NULL,
        created_by   TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        PRIMARY KEY (case_id, kind, version),
        FOREIGN KEY (case_id) REFERENCES decision_cases(case_id)
    )
    """,
    # 版本表 append-only（本任務表範圍內的 DB 層保證）：
    # UPDATE／DELETE 一律由 trigger 拒絕，因此「新版本後舊 run 永久 stale」
    # 的前提（版本不可刪退）也由 DB 層守住。
    """
    CREATE TRIGGER IF NOT EXISTS trg_decision_case_versions_no_update
    BEFORE UPDATE ON decision_case_versions
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'decision_case_versions is append-only: UPDATE rejected');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_decision_case_versions_no_delete
    BEFORE DELETE ON decision_case_versions
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'decision_case_versions is append-only: DELETE rejected');
    END
    """,
    # REPLACE 覆寫防護（不依賴 recursive_triggers）：
    # BEFORE INSERT 在 REPLACE 的隱式刪除之前執行（實測於 rt=0/1、重連不
    # init 的連線），直接對「同 (case_id, kind, version) 的既有列」與
    # 「NEW 明確指定的 rowid 撞上既有列」RAISE(ABORT)，因此 INSERT OR
    # REPLACE 無法刪退歷史或覆寫內容。recursive_triggers 保留為雙層防守，
    # 不再作為唯一保護。
    """
    CREATE TRIGGER IF NOT EXISTS trg_decision_case_versions_no_replace
    BEFORE INSERT ON decision_case_versions
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1 FROM decision_case_versions AS v
        WHERE v.case_id = NEW.case_id
          AND v.kind    = NEW.kind
          AND v.version = NEW.version
    ) OR (
        NEW.rowid IS NOT NULL AND EXISTS (
            SELECT 1 FROM decision_case_versions AS v
            WHERE v.rowid = NEW.rowid
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'decision_case_versions is append-only: '
                            || 'INSERT OR REPLACE conflict rejected');
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_case_scenario_runs (
        run_id        TEXT PRIMARY KEY,
        case_id       TEXT NOT NULL,
        input_version INTEGER NOT NULL CHECK (input_version > 0),
        result_json   TEXT NOT NULL,
        created_by    TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        FOREIGN KEY (case_id) REFERENCES decision_cases(case_id)
    )
    """,
    # run 引用保護：INSERT 時被引用的 scenario_input 版本必須存在
    # （與 INSERT 同一語句邊界內由 SQLite 保證，不會有 check-then-insert 時窗）。
    """
    CREATE TRIGGER IF NOT EXISTS trg_decision_case_runs_valid_ref
    BEFORE INSERT ON decision_case_scenario_runs
    FOR EACH ROW
    WHEN NOT EXISTS (
        SELECT 1 FROM decision_case_versions
        WHERE case_id = NEW.case_id
          AND kind = 'scenario_input'
          AND version = NEW.input_version
    )
    BEGIN
        SELECT RAISE(ABORT, 'scenario run references a missing scenario_input version');
    END
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_decision_case_runs_case
        ON decision_case_scenario_runs(case_id)
    """,
)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the decision_case_* tables on the given connection only.

    Idempotent; callers re-invoke it after reopening a connection so that
    ``PRAGMA foreign_keys`` is enforced per connection.

    Fail closed: if foreign keys or recursive triggers cannot be enabled, raise
    :class:`DecisionCaseDataError` instead of pretending the schema is safe.
    (recursive_triggers is defense-in-depth only: REPLACE overwrite protection
    comes from the persisted BEFORE INSERT trigger and does not depend on it.)
    Never commits or rolls back the caller's transaction.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    (enabled,) = conn.execute("PRAGMA foreign_keys").fetchone()
    if not enabled:
        raise DecisionCaseDataError(
            "此連線無法啟用外鍵（可能位於既有交易內）；"
            "拒絕初始化 schema，不偷偷 commit 呼叫端交易（fail closed）"
        )
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)
    (enabled,) = conn.execute("PRAGMA foreign_keys").fetchone()
    if not enabled:
        raise DecisionCaseDataError(
            "初始化後外鍵仍未啟用；拒絕使用此連線（fail closed）"
        )
    (recursive,) = conn.execute("PRAGMA recursive_triggers").fetchone()
    if not recursive:
        raise DecisionCaseDataError(
            "此連線無法啟用 recursive_triggers；REPLACE 覆寫已由 BEFORE "
            "INSERT trigger 阻擋，此 PRAGMA 僅為雙層防守，仍拒絕使用此連線 "
            "（fail closed）"
        )


# ── strict JSON ──────────────────────────────────────────────────────────────

def _ensure_finite(value: object) -> None:
    """Reject NaN / Infinity / overflow-to-inf anywhere in a decoded value."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, float):
            if not math.isfinite(item):
                raise DecisionCaseDataError(
                    "JSON 含非有限數值（NaN／Infinity／數值溢位）"
                )
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)


def encode_json(obj: object) -> str:
    try:
        return json.dumps(
            obj, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise DecisionCaseDataError(f"內容無法序列化為 JSON：{exc}") from exc


def decode_json(text: str) -> object:
    def _reject_constant(name: str) -> None:
        raise DecisionCaseDataError(f"儲存的 JSON 含非有限常數 {name}")

    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except DecisionCaseDataError:
        raise
    except (json.JSONDecodeError, TypeError) as exc:
        raise DecisionCaseDataError(f"儲存的 JSON 已損壞：{exc}") from exc
    _ensure_finite(value)  # 涵蓋 1e999 → inf 這類解析溢位
    return value


def decode_json_object(text: str) -> dict:
    """Parse stored JSON and require a JSON object (dict); anything else fails closed."""
    value = decode_json(text)
    if not isinstance(value, dict):
        raise DecisionCaseDataError(
            f"內容必須是 JSON object，實際收到 {type(value).__name__}"
        )
    return value


# ── cases ──────────────────────────────────────────────────────────────────

def insert_case(
    conn: sqlite3.Connection, case_id: str, title: str, created_by: str, created_at: str
) -> None:
    conn.execute(
        "INSERT INTO decision_cases (case_id, title, status, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (case_id, title, _CASE_STATUS_OPEN, created_by, created_at),
    )


def get_case_row(conn: sqlite3.Connection, case_id: str) -> tuple | None:
    return conn.execute(
        "SELECT case_id, title, status, created_by, created_at "
        "FROM decision_cases WHERE case_id = ?",
        (case_id,),
    ).fetchone()


def case_exists(conn: sqlite3.Connection, case_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM decision_cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        is not None
    )


# ── events ─────────────────────────────────────────────────────────────────

def insert_event(
    conn: sqlite3.Connection,
    event_id: str,
    case_id: str,
    event_type: str,
    region: str,
    country: str,
    description: str,
    occurred_at: str,
    recorded_by: str,
    recorded_at: str,
) -> None:
    conn.execute(
        "INSERT INTO decision_case_events "
        "(event_id, case_id, event_type, region, country, description, "
        " occurred_at, recorded_by, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            case_id,
            event_type,
            region,
            country,
            description,
            occurred_at,
            recorded_by,
            recorded_at,
        ),
    )


def list_event_rows(conn: sqlite3.Connection, case_id: str) -> list[tuple]:
    return conn.execute(
        "SELECT event_id, case_id, event_type, region, country, description, "
        "       occurred_at, recorded_by, recorded_at "
        "FROM decision_case_events WHERE case_id = ? ORDER BY rowid",
        (case_id,),
    ).fetchall()


# ── exposure mappings ──────────────────────────────────────────────────────

def insert_mapping(
    conn: sqlite3.Connection,
    mapping_id: str,
    case_id: str,
    subject_type: str,
    subject_key: str,
    status: str,
    rationale: str,
    created_by: str,
    created_at: str,
) -> None:
    if status not in VALID_MAPPING_STATUSES:
        raise DecisionCaseDataError(f"非法曝險狀態：{status!r}")
    conn.execute(
        "INSERT INTO decision_case_exposure_mappings "
        "(mapping_id, case_id, subject_type, subject_key, status, rationale, "
        " created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            mapping_id,
            case_id,
            subject_type,
            subject_key,
            status,
            rationale,
            created_by,
            created_at,
        ),
    )


def list_mapping_rows(conn: sqlite3.Connection, case_id: str) -> list[tuple]:
    return conn.execute(
        "SELECT mapping_id, case_id, subject_type, subject_key, status, "
        "       rationale, created_by, created_at "
        "FROM decision_case_exposure_mappings WHERE case_id = ? ORDER BY rowid",
        (case_id,),
    ).fetchall()


def find_mapping_row(
    conn: sqlite3.Connection, case_id: str, subject_type: str, subject_key: str
) -> tuple | None:
    """Look up the single mapping for a subject.

    唯一索引已從寫入端杜絕矛盾資料；若既有資料仍出現多筆（例如索引建立前
    的歷史資料），明確報資料歧義，絕不靠 fetchone 的無序語意矇混過關。
    """
    rows = conn.execute(
        "SELECT mapping_id, case_id, subject_type, subject_key, status, "
        "       rationale, created_by, created_at "
        "FROM decision_case_exposure_mappings "
        "WHERE case_id = ? AND subject_type = ? AND subject_key = ?",
        (case_id, subject_type, subject_key),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise DecisionCaseDataError(
            f"case {case_id!r} 的 ({subject_type!r}, {subject_key!r}) 存在 "
            f"{len(rows)} 筆矛盾 mapping（fail closed）"
        )
    return rows[0]


def get_mapping_row(conn: sqlite3.Connection, mapping_id: str) -> tuple | None:
    row = conn.execute(
        "SELECT mapping_id, case_id, subject_type, subject_key, status, "
        "       rationale, created_by, created_at "
        "FROM decision_case_exposure_mappings WHERE mapping_id = ?",
        (mapping_id,),
    ).fetchone()
    if row is None:
        return None
    if not case_exists(conn, row[1]):
        raise DecisionCaseDataError(
            f"mapping {mapping_id!r} 的父案件 {row[1]!r} 已不存在（fail closed）"
        )
    return row


# ── append-only versions ───────────────────────────────────────────────────

def max_version(conn: sqlite3.Connection, case_id: str, kind: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(version) FROM decision_case_versions "
        "WHERE case_id = ? AND kind = ?",
        (case_id, kind),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def insert_version(
    conn: sqlite3.Connection,
    case_id: str,
    kind: str,
    version: int,
    content_json: str,
    created_by: str,
    created_at: str,
) -> None:
    if kind not in VALID_VERSION_KINDS:
        raise DecisionCaseDataError(f"非法版本種類：{kind!r}")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise DecisionCaseDataError(f"非法版本號：{version!r}")
    decode_json_object(content_json)  # 寫入前先驗證內容為有效 JSON object
    conn.execute(
        "INSERT INTO decision_case_versions "
        "(case_id, kind, version, content_json, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (case_id, kind, version, content_json, created_by, created_at),
    )


def append_version_atomic(
    conn: sqlite3.Connection,
    case_id: str,
    kind: str,
    content_json: str,
    created_by: str,
    created_at: str,
) -> int:
    """Append the next version and return its number.

    編號與插入是同一條 INSERT…SELECT…RETURNING 語句：不存在
    SELECT MAX 與 INSERT 之間的競爭時窗；並行合法追加各自拿到不同版本號。
    不 commit／rollback 呼叫端交易。
    """
    if kind not in VALID_VERSION_KINDS:
        raise DecisionCaseDataError(f"非法版本種類：{kind!r}")
    decode_json_object(content_json)  # strict JSON 驗證
    row = conn.execute(
        "INSERT INTO decision_case_versions "
        "(case_id, kind, version, content_json, created_by, created_at) "
        "SELECT ?, ?, COALESCE(MAX(version), 0) + 1, ?, ?, ? "
        "FROM decision_case_versions WHERE case_id = ? AND kind = ? "
        "RETURNING version",
        (case_id, kind, content_json, created_by, created_at, case_id, kind),
    ).fetchone()
    if row is None:
        raise DecisionCaseDataError("版本 INSERT 未回傳版本號（fail closed）")
    return int(row[0])


def list_version_rows(
    conn: sqlite3.Connection, case_id: str, kind: str | None = None
) -> list[tuple]:
    """Return (case_id, kind, version, content_json, created_by, created_at) rows."""
    if kind is None:
        return conn.execute(
            "SELECT case_id, kind, version, content_json, created_by, created_at "
            "FROM decision_case_versions WHERE case_id = ? ORDER BY kind, version",
            (case_id,),
        ).fetchall()
    return conn.execute(
        "SELECT case_id, kind, version, content_json, created_by, created_at "
        "FROM decision_case_versions WHERE case_id = ? AND kind = ? "
        "ORDER BY version",
        (case_id, kind),
    ).fetchall()


# ── scenario run references ────────────────────────────────────────────────

# stale 是由版本關係直接導出的不可逆事實：run 的 input_version 小於該 case
# 目前 scenario_input 的最新版本即為 stale。版本表是 append-only（UPDATE／
# DELETE 由本任務 trigger 拒絕），最新版本只會增加、永不回退，因此舊 run
# 一旦 stale 就不可能因為後續任何新 run 而重新有效。
_STALE_EXPR = (
    "CASE WHEN r.input_version < ("
    "    SELECT MAX(v.version) FROM decision_case_versions v "
    "    WHERE v.case_id = r.case_id AND v.kind = 'scenario_input'"
    ") THEN 1 ELSE 0 END"
)


def _verify_run_reference(
    conn: sqlite3.Connection, case_id: str, input_version: int
) -> None:
    """run 必須引用同 case 的 scenario_input 精確版本，且內容有效；
    缺失或損壞一律 DecisionCaseDataError（fail closed）。"""
    row = conn.execute(
        "SELECT content_json FROM decision_case_versions "
        "WHERE case_id = ? AND kind = ? AND version = ?",
        (case_id, KIND_SCENARIO_INPUT, input_version),
    ).fetchone()
    if row is None:
        raise DecisionCaseDataError(
            f"case {case_id!r} 的 scenario_input 第 {input_version} 版不存在，"
            "scenario run 引用已失效（fail closed）"
        )
    decode_json_object(row[0])  # 損壞內容 → DecisionCaseDataError


def _verify_run_row(conn: sqlite3.Connection, row: tuple) -> None:
    if not case_exists(conn, row[1]):
        raise DecisionCaseDataError(
            f"run {row[0]!r} 的父案件 {row[1]!r} 已不存在（fail closed）"
        )
    _verify_run_reference(conn, row[1], row[2])


def insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    case_id: str,
    input_version: int,
    result_json: str,
    created_by: str,
    created_at: str,
) -> None:
    if isinstance(input_version, bool) or not isinstance(input_version, int) or input_version < 1:
        raise DecisionCaseDataError(f"非法 input_version：{input_version!r}")
    decode_json_object(result_json)
    # 引用檢查：不只驗證存在，也驗證被引用輸入的內容有效。
    # INSERT 本身另由 trg_decision_case_runs_valid_ref 在 DB 語意上保護，
    # 兩者之間不靠 Python 層原子性。
    _verify_run_reference(conn, case_id, input_version)
    conn.execute(
        "INSERT INTO decision_case_scenario_runs "
        "(run_id, case_id, input_version, result_json, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, case_id, input_version, result_json, created_by, created_at),
    )


def list_run_rows(conn: sqlite3.Connection, case_id: str) -> list[tuple]:
    rows = conn.execute(
        "SELECT r.run_id, r.case_id, r.input_version, r.result_json, "
        f"       {_STALE_EXPR} AS is_stale, r.created_by, r.created_at "
        "FROM decision_case_scenario_runs r WHERE r.case_id = ? ORDER BY r.rowid",
        (case_id,),
    ).fetchall()
    for row in rows:
        _verify_run_row(conn, row)  # 缺失／損壞引用 → DecisionCaseDataError
    return rows


def get_run_row(conn: sqlite3.Connection, run_id: str) -> tuple | None:
    row = conn.execute(
        "SELECT r.run_id, r.case_id, r.input_version, r.result_json, "
        f"       {_STALE_EXPR} AS is_stale, r.created_by, r.created_at "
        "FROM decision_case_scenario_runs r WHERE r.run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    _verify_run_row(conn, row)
    return row
