# AI Agent 治理觀測指標

本文件記錄目前四個 SQLite View 的計算方式、用途與限制。這些指標是專案依現有 log 欄位定義的流程觀測 baseline，不是 NIST、OWASP、ISO 或 EU AI Act 規定的標準 KPI，也不能單獨證明治理控制有效。

## 指標定義

| 指標 | View | 現行計算方式 | 可支持的解讀 |
|---|---|---|---|
| 平均決策時間 | `view_decision_time` | 相同 caller 在 dispatch 後 120 秒內之 action 平均時間差 | 粗略流程延遲 proxy |
| pending 送審比例 | `view_pending_intercept_ratio` | `needs_approval = 1` 的 dispatch 數 / 全部 dispatch 數 | 送審工作量與流程分布；不是攻擊攔截成功率 |
| 紀錄關聯率 | `view_traceability_rate` | 以相同 caller 與 120 秒時間窗找到可能上游 dispatch 的 action 比例 | 粗略關聯 proxy；不是完整端到端追溯率 |
| 平均帶工具數 | `view_avg_tools_per_turn` | 關聯 action 數 / 不重複 dispatch 數 | 工具使用量的粗略觀測值 |

四個 View 在分母為零或沒有可匹配觀測資料時回傳 `NULL`，Dashboard 顯示「資料不足」。空資料不應被呈現為 `0%`、`100%` 或固定延遲。

## 已知限制

- 現行 dispatch/action 關聯依賴 caller 與 120 秒時間窗；並行或連續任務可能錯配。
- `needs_approval` 來自路由紀錄，未驗證該項操作是否按照 ground truth 應被送審。
- 目前沒有唯一 trace id，因此不能準確計算端到端 trace coverage 或延遲分位數。
- 沒有經標記的合法、越權與攻擊案例集，因此尚不能由這四個 View 推導 policy accuracy、false-block rate 或 attack success rate。

## 正式評估前置條件

1. 為 request、dispatch、tool action 與 approval 傳遞同一個唯一 trace id。
2. 建立含 `execute`、`pending`、`deny` ground truth 的測試集。
3. 將正常業務案例與攻擊案例分開報告。
4. 同時量測 policy decision accuracy、attack success rate、legitimate-action false-block rate、utility under attack 與 latency。
5. 將指標定義、版本、資料來源、分子分母、空資料規則與限制登記於 metric registry。

實作位置：[`backend/database.py`](../backend/database.py)；顯示位置：[`frontend/page_agent_dashboard.py`](../frontend/page_agent_dashboard.py)；測試：[`tests/test_metrics_views.py`](../tests/test_metrics_views.py)。
