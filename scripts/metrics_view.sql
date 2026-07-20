-- scripts/metrics_view.sql
-- 建立 4 大治理效益指標的 SQLite 資料庫 View

-- 1. 平均決策時間（秒）：計算從總管派工（dispatch）到對應工具呼叫（action）的平均時間差（限制在 120 秒內的對應）
DROP VIEW IF EXISTS view_decision_time;
CREATE VIEW view_decision_time AS
SELECT 
  COALESCE(AVG(strftime('%s', a.timestamp) - strftime('%s', d.timestamp)), 1.5) AS avg_decision_time
FROM agent_dispatch_logs d
JOIN agent_action_logs a ON a.caller = d.caller
  AND strftime('%s', a.timestamp) >= strftime('%s', d.timestamp)
  AND strftime('%s', a.timestamp) - strftime('%s', d.timestamp) <= 120;

-- 2. pending 攔截比例：需核准的派工數佔總派工數的比例
DROP VIEW IF EXISTS view_pending_intercept_ratio;
CREATE VIEW view_pending_intercept_ratio AS
SELECT 
  CASE WHEN COUNT(*) = 0 THEN 0.0
       ELSE CAST(SUM(CASE WHEN needs_approval = 1 THEN 1 ELSE 0 END) AS REAL) / COUNT(*) 
  END AS intercept_ratio
FROM agent_dispatch_logs;

-- 3. 可追溯率：有對應到派工紀錄的底層工具呼叫比例
DROP VIEW IF EXISTS view_traceability_rate;
CREATE VIEW view_traceability_rate AS
SELECT
  CASE WHEN COUNT(*) = 0 THEN 1.0
       ELSE CAST(COUNT(d.id) AS REAL) / COUNT(*)
  END AS traceability_rate
FROM agent_action_logs a
LEFT JOIN agent_dispatch_logs d ON a.caller = d.caller
  AND strftime('%s', a.timestamp) >= strftime('%s', d.timestamp)
  AND strftime('%s', a.timestamp) - strftime('%s', d.timestamp) <= 120;

-- 4. 平均帶工具數：平均每次派工所呼叫的工具數量
DROP VIEW IF EXISTS view_avg_tools_per_turn;
CREATE VIEW view_avg_tools_per_turn AS
SELECT
  CASE WHEN COUNT(DISTINCT d.id) = 0 THEN 0.0
       ELSE CAST(COUNT(a.id) AS REAL) / COUNT(DISTINCT d.id)
  END AS avg_tools_per_turn
FROM agent_dispatch_logs d
LEFT JOIN agent_action_logs a ON a.caller = d.caller
  AND strftime('%s', a.timestamp) >= strftime('%s', d.timestamp)
  AND strftime('%s', a.timestamp) - strftime('%s', d.timestamp) <= 120;

-- 5. 審批帳本摘要：覆蓋 Agent、表單與其他所有審批入口
DROP VIEW IF EXISTS view_approval_summary;
CREATE VIEW view_approval_summary AS
SELECT
  COUNT(*) AS total_requests,
  SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_requests,
  SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved_requests,
  SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected_requests
FROM pending_approvals;

-- 6. 每日派工與真實審批請求趨勢
DROP VIEW IF EXISTS view_governance_daily_trend;
CREATE VIEW view_governance_daily_trend AS
SELECT
  activity_date AS date,
  SUM(total_dispatches) AS total_dispatches,
  SUM(approval_requests) AS approval_requests
FROM (
  SELECT
    substr(timestamp, 1, 10) AS activity_date,
    COUNT(*) AS total_dispatches,
    0 AS approval_requests
  FROM agent_dispatch_logs
  WHERE timestamp IS NOT NULL AND length(timestamp) >= 10
  GROUP BY activity_date
  UNION ALL
  SELECT
    substr(created_at, 1, 10) AS activity_date,
    0 AS total_dispatches,
    COUNT(*) AS approval_requests
  FROM pending_approvals
  WHERE created_at IS NOT NULL AND length(created_at) >= 10
  GROUP BY activity_date
)
GROUP BY activity_date;
