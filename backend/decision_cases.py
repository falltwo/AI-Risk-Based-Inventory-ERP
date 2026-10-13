"""最小 Decision Case service：保存事件證據、曝險對映、append-only 輸入版本與
scenario run 引用。

範圍記錄（本次任務）：
- 只交付「案件持久化」。proposal／approval／receipt 尚未接上，也未整合任何
  情境引擎 —— 本模組只保存 case 與 scenario run 的「結果引用」。
- 未命中（miss）永遠停留在 unknown／unmapped，絕不標成「安全」；
  抽取失敗只會被拒絕，不會轉成正式風險。

授權契約：完全沿用 backend/access_control 的既有 capability，不新增能力、
不擴張角色權限：
- 讀取（get_case / list_*）：``risk.analysis.read``
- 寫入（create / add / append / record）：``risk.workspace.write``
- 每次操作都在「明確傳入的授權連線」（``auth_conn``，預設同案件連線）上
  用既有的 ``load_principal`` 重新解析 principal：拒絕空 username、拒絕
  身分不存在或 entitlement 已失效、拒絕重新解析後的組織身分與原
  principal 不符（相同 username 不得跨部署更換身分）、拒絕授權連線與
  案件 store 分屬不同部署組織（案件 store 的 ``app_metadata.deployment_organization_id`` 必須
  等於 principal 的 organization_id）。不信任傳入 AccessContext 的快取
  capabilities、不 fallback 正式 DB、不依賴 Streamlit session。
- 持久化的記錄者只能是已驗證 principal.username；``actor`` 參數只接受
  相同身分，冒用他人身分一律 PermissionDeniedError。

append-only 保證範圍：版本表在本任務 schema 內由 trigger 拒絕 UPDATE／
DELETE（見 decision_case_repository），屬 DB 層保證；不宣稱能抵抗 DBA
改 schema。版本編號由單一原子 INSERT…SELECT…RETURNING 語句產生。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3

from backend.access_control import (
    RISK_ANALYSIS_READ,
    RISK_WORKSPACE_WRITE,
    AccessContext,
    load_principal,
)
from backend import decision_case_repository as repo
from backend.decision_case_repository import (
    DecisionCaseDataError,
    KIND_MANUAL_CORRECTION,
    KIND_RAW_EVIDENCE,
    KIND_SCENARIO_INPUT,
    STATUS_CANDIDATE,
    STATUS_CONFIRMED,
    STATUS_UNKNOWN,
    STATUS_UNMAPPED,
    VALID_MAPPING_STATUSES,
    VALID_VERSION_KINDS,
)

__all__ = [
    "DecisionCaseDataError",
    "KIND_MANUAL_CORRECTION",
    "KIND_RAW_EVIDENCE",
    "KIND_SCENARIO_INPUT",
    "STATUS_CANDIDATE",
    "STATUS_CONFIRMED",
    "STATUS_UNKNOWN",
    "STATUS_UNMAPPED",
    "VALID_MAPPING_STATUSES",
    "VALID_VERSION_KINDS",
    "PermissionDeniedError",
    "CaseNotFoundError",
    "DecisionCase",
    "CaseEvent",
    "ExposureMapping",
    "VersionedContent",
    "ScenarioRunRef",
    "DecisionCaseService",
]


class PermissionDeniedError(PermissionError):
    """principal 缺少既有 capability 或越過身分／部署邊界時拋出（fail closed）。"""


class CaseNotFoundError(LookupError):
    """case_id 不存在時拋出；不給空清單冒充成功。"""


@dataclass(frozen=True)
class DecisionCase:
    case_id: str
    title: str
    status: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class CaseEvent:
    event_id: str
    case_id: str
    event_type: str
    region: str
    country: str
    description: str
    occurred_at: str
    recorded_by: str
    recorded_at: str


@dataclass(frozen=True)
class ExposureMapping:
    mapping_id: str
    case_id: str
    subject_type: str
    subject_key: str
    status: str
    rationale: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class VersionedContent:
    case_id: str
    kind: str
    version: int
    content: dict
    created_by: str
    created_at: str


@dataclass(frozen=True)
class ScenarioRunRef:
    run_id: str
    case_id: str
    input_version: int
    result: dict
    stale: bool
    created_by: str
    created_at: str


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value: object) -> str:
    return str(value or "").strip()


class DecisionCaseService:
    """決策案件的最小讀寫 service。

    每次建構都要求：
    - ``conn``：明確傳入的 SQLite connection（本 service 不自行開關連線）；
    - ``principal``：明確傳入的 AccessContext；為 None 或無法在授權連線
      重新解析時所有操作 fail closed 拋出 PermissionDeniedError；
    - ``auth_conn``：明確傳入的授權連線（預設同 ``conn``）。每次操作都在
      此連線上以既有 load_principal 重新解析 principal 與 entitlement，
      不信任 AccessContext 的快取 capabilities。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        principal: AccessContext | None,
        *,
        auth_conn: sqlite3.Connection | None = None,
    ):
        if conn is None:
            raise ValueError("conn 不可為 None")
        if auth_conn is None:
            auth_conn = conn
        self._conn = conn
        self._auth_conn = auth_conn
        self._principal = principal
        self._verified_principal: AccessContext | None = None

    # ── 授權 ──────────────────────────────────────────────────────────────

    def _require(self, capability: str) -> None:
        """在明確授權連線上重新解析 principal 並檢查 capability。

        拒絕：principal 為 None、空 username、授權連線無法解析（身分不
        存在／部署不符／entitlement 失效）、授權連線與案件 store 的部署
        組織不一致、capability 不足。
        """
        if self._principal is None:
            raise PermissionDeniedError(f"缺少既有 capability：{capability}")
        username = _clean(self._principal.username)
        if not username:
            raise PermissionDeniedError(
                "principal username 為空，拒絕操作（fail closed）"
            )
        fresh = load_principal(username, conn=self._auth_conn)
        if fresh is None:
            raise PermissionDeniedError(
                f"principal {username!r} 無法在授權連線重新解析"
                "（身分不存在／部署不符／entitlement 已失效）"
            )
        # 原 principal 的組織／部署身分必須被保留：相同 username 不能作為
        # 跨部署更換 principal 的依據。重新解析結果若與原 principal 的組織
        # 身分不符，一律 fail closed（同連線與分離 auth_conn 路徑皆適用）。
        if _clean(self._principal.organization_id) != _clean(fresh.organization_id):
            raise PermissionDeniedError(
                f"重新解析後的 principal 組織身分（{fresh.organization_id!r}）"
                f"與原 principal（{self._principal.organization_id!r}）不符，"
                "拒絕操作（fail closed）"
            )
        if self._auth_conn is not self._conn:
            deployment = self._conn.execute(
                "SELECT value FROM app_metadata "
                "WHERE key = 'deployment_organization_id'"
            ).fetchone()
            if deployment is None or deployment[0] != fresh.organization_id:
                raise PermissionDeniedError(
                    "案件 store 的部署組織與 principal 組織不符，"
                    "拒絕操作（fail closed）"
                )
        if not fresh.can(capability):
            raise PermissionDeniedError(f"缺少既有 capability：{capability}")
        self._verified_principal = fresh

    def _actor(self, actor: str | None) -> str:
        """記錄者只能是已驗證 principal.username；冒用他人身分一律拒絕。"""
        assert self._verified_principal is not None, "寫入路徑必先過 _require"
        username = self._verified_principal.username
        if actor is not None:
            provided = _clean(actor)
            if provided != username:
                raise PermissionDeniedError(
                    f"記錄者只能是已驗證 principal {username!r}，"
                    f"不得冒用為 {provided!r}"
                )
        return username

    # ── 寫入（需要 risk.workspace.write）──────────────────────────────────

    def create_case(
        self, case_id: str, title: str, *, actor: str | None = None
    ) -> DecisionCase:
        self._require(RISK_WORKSPACE_WRITE)
        case_id = _clean(case_id)
        title = _clean(title)
        if not case_id or not title:
            raise ValueError("case_id 與 title 不可為空")
        created_by = self._actor(actor)
        created_at = _utcnow_iso()
        repo.insert_case(self._conn, case_id, title, created_by, created_at)
        return DecisionCase(case_id, title, "open", created_by, created_at)

    def add_event(
        self,
        event_id: str,
        case_id: str,
        event_type: str,
        region: str = "",
        country: str = "",
        description: str = "",
        occurred_at: str = "",
        *,
        actor: str | None = None,
    ) -> CaseEvent:
        self._require(RISK_WORKSPACE_WRITE)
        event_id = _clean(event_id)
        case_id = _clean(case_id)
        event_type = _clean(event_type)
        if not event_id or not case_id or not event_type:
            raise ValueError("event_id、case_id、event_type 不可為空")
        self._require_case(case_id)
        recorded_by = self._actor(actor)
        recorded_at = _utcnow_iso()
        repo.insert_event(
            self._conn,
            event_id,
            case_id,
            event_type,
            region or "",
            country or "",
            description or "",
            occurred_at or "",
            recorded_by,
            recorded_at,
        )
        return CaseEvent(
            event_id,
            case_id,
            event_type,
            region or "",
            country or "",
            description or "",
            occurred_at or "",
            recorded_by,
            recorded_at,
        )

    def add_mapping(
        self,
        mapping_id: str,
        case_id: str,
        subject_type: str,
        subject_key: str,
        status: str,
        rationale: str = "",
        *,
        actor: str | None = None,
    ) -> ExposureMapping:
        self._require(RISK_WORKSPACE_WRITE)
        mapping_id = _clean(mapping_id)
        case_id = _clean(case_id)
        subject_type = _clean(subject_type)
        subject_key = _clean(subject_key)
        if not (mapping_id and case_id and subject_type and subject_key):
            raise ValueError("mapping_id、case_id、subject_type、subject_key 不可為空")
        if status not in VALID_MAPPING_STATUSES:
            raise ValueError(
                f"非法曝險狀態：{status!r}；只接受 {sorted(VALID_MAPPING_STATUSES)}"
            )
        self._require_case(case_id)
        created_by = self._actor(actor)
        created_at = _utcnow_iso()
        repo.insert_mapping(
            self._conn,
            mapping_id,
            case_id,
            subject_type,
            subject_key,
            status,
            rationale or "",
            created_by,
            created_at,
        )
        return ExposureMapping(
            mapping_id,
            case_id,
            subject_type,
            subject_key,
            status,
            rationale or "",
            created_by,
            created_at,
        )

    def append_version(
        self,
        case_id: str,
        kind: str,
        content: dict,
        *,
        actor: str | None = None,
    ) -> VersionedContent:
        """Append-only：新內容成為下一版，歷史版本內容永不覆寫。

        版本號由 repository 的單一原子 INSERT…SELECT…RETURNING 語句產生。
        內容只序列化一次：持久化與回傳物件都解碼自同一 canonical JSON
        字串，呼叫端對原 dict 的巢狀變動不會影響已保存的快照。
        """
        self._require(RISK_WORKSPACE_WRITE)
        case_id = _clean(case_id)
        if not case_id:
            raise ValueError("case_id 不可為空")
        if kind not in VALID_VERSION_KINDS:
            raise ValueError(f"非法版本種類：{kind!r}")
        if not isinstance(content, dict):
            raise ValueError("版本內容必須是 dict")
        self._require_case(case_id)
        created_by = self._actor(actor)
        created_at = _utcnow_iso()
        payload = repo.encode_json(content)  # strict JSON（allow_nan=False）
        version = repo.append_version_atomic(
            self._conn, case_id, kind, payload, created_by, created_at
        )
        snapshot = repo.decode_json_object(payload)
        return VersionedContent(
            case_id, kind, version, snapshot, created_by, created_at
        )

    def record_scenario_run(
        self,
        case_id: str,
        run_id: str,
        input_version: int,
        result: dict,
        *,
        actor: str | None = None,
    ) -> ScenarioRunRef:
        """保存 scenario run 的結果引用。

        run 必須引用當時已存在的 scenario_input 版本，且該版本內容有效；
        之後任何新輸入版本都會讓這個 run 永久 stale（版本表為 append-only，
        最新版本只增不減，因此 stale 由版本關係導出、不可逆）。
        """
        self._require(RISK_WORKSPACE_WRITE)
        case_id = _clean(case_id)
        run_id = _clean(run_id)
        if not case_id or not run_id:
            raise ValueError("case_id 與 run_id 不可為空")
        if not isinstance(result, dict):
            raise ValueError("run 結果必須是 dict")
        self._require_case(case_id)
        created_by = self._actor(actor)
        created_at = _utcnow_iso()
        repo.insert_run(
            self._conn,
            run_id,
            case_id,
            input_version,
            repo.encode_json(result),
            created_by,
            created_at,
        )
        row = repo.get_run_row(self._conn, run_id)
        assert row is not None, "剛寫入的 run 讀不回（不可能，除錯用）"
        return self._run_from_row(row)

    # ── 讀取（需要 risk.analysis.read）────────────────────────────────────

    def _require_case(self, case_id: str) -> None:
        if not repo.case_exists(self._conn, case_id):
            raise CaseNotFoundError(f"case 不存在：{case_id!r}")

    def get_case(self, case_id: str) -> DecisionCase:
        self._require(RISK_ANALYSIS_READ)
        row = repo.get_case_row(self._conn, _clean(case_id))
        if row is None:
            raise CaseNotFoundError(f"case 不存在：{_clean(case_id)!r}")
        return DecisionCase(*row)

    def list_events(self, case_id: str) -> list[CaseEvent]:
        self._require(RISK_ANALYSIS_READ)
        case_id = _clean(case_id)
        self._require_case(case_id)
        return [CaseEvent(*row) for row in repo.list_event_rows(self._conn, case_id)]

    def list_mappings(self, case_id: str) -> list[ExposureMapping]:
        self._require(RISK_ANALYSIS_READ)
        case_id = _clean(case_id)
        self._require_case(case_id)
        return [
            ExposureMapping(*row)
            for row in repo.list_mapping_rows(self._conn, case_id)
        ]

    def get_mapping(self, mapping_id: str) -> ExposureMapping | None:
        self._require(RISK_ANALYSIS_READ)
        row = repo.get_mapping_row(self._conn, _clean(mapping_id))
        return None if row is None else ExposureMapping(*row)

    def find_mapping(
        self, case_id: str, subject_type: str, subject_key: str
    ) -> ExposureMapping | None:
        """未命中回傳 None —— 不建立任何列、不升格成任何正式曝險。

        讀寫採用同一 subject 正規化（strip）。
        """
        self._require(RISK_ANALYSIS_READ)
        case_id = _clean(case_id)
        self._require_case(case_id)
        row = repo.find_mapping_row(
            self._conn, case_id, _clean(subject_type), _clean(subject_key)
        )
        return None if row is None else ExposureMapping(*row)

    def mapping_status_for(
        self, case_id: str, subject_type: str, subject_key: str
    ) -> str:
        """對映狀態查詢；未命中固定回傳 unmapped（絕不標成 safe）。"""
        mapping = self.find_mapping(case_id, subject_type, subject_key)
        return mapping.status if mapping is not None else STATUS_UNMAPPED

    def list_versions(
        self, case_id: str, kind: str | None = None
    ) -> list[VersionedContent]:
        self._require(RISK_ANALYSIS_READ)
        case_id = _clean(case_id)
        self._require_case(case_id)
        if kind is not None and kind not in VALID_VERSION_KINDS:
            raise ValueError(f"非法版本種類：{kind!r}")
        rows = repo.list_version_rows(self._conn, case_id, kind)
        return [
            VersionedContent(
                row[0],
                row[1],
                row[2],
                repo.decode_json_object(row[3]),
                row[4],
                row[5],
            )
            for row in rows
        ]

    def latest_version(self, case_id: str, kind: str) -> int | None:
        self._require(RISK_ANALYSIS_READ)
        case_id = _clean(case_id)
        self._require_case(case_id)
        if kind not in VALID_VERSION_KINDS:
            raise ValueError(f"非法版本種類：{kind!r}")
        return repo.max_version(self._conn, case_id, kind)

    def list_runs(self, case_id: str) -> list[ScenarioRunRef]:
        self._require(RISK_ANALYSIS_READ)
        case_id = _clean(case_id)
        self._require_case(case_id)
        return [
            self._run_from_row(row)
            for row in repo.list_run_rows(self._conn, case_id)
        ]

    def get_run(self, run_id: str) -> ScenarioRunRef | None:
        self._require(RISK_ANALYSIS_READ)
        row = repo.get_run_row(self._conn, _clean(run_id))
        return None if row is None else self._run_from_row(row)

    @staticmethod
    def _run_from_row(row: tuple) -> ScenarioRunRef:
        return ScenarioRunRef(
            row[0],
            row[1],
            row[2],
            repo.decode_json_object(row[3]),
            bool(row[4]),
            row[5],
            row[6],
        )
