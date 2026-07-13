# B｜總管 Agent — Agent 名單 & 路由規則（Day 1 上午交付）

> 負責人：組員 B（總管 Agent / 組長）
> 交付物：**Agent 名單**、**路由規則表**、**總管 Agent system prompt 草稿**
> 對應程式：[`backend/agent_registry.py`](../backend/agent_registry.py)（雛型，Day 2 的 `agent_orchestrator.py` 會接這份）

---

## 0. 這份文件在整體架構的位置

```
使用者 / LINE
      │  任務（自然語言）
      ▼
┌─────────────────────────┐
│      總管 Agent          │  ← 本文件負責：判斷任務 → 派給哪個專責 Agent
│  (agent_orchestrator)   │
└───────────┬─────────────┘
            │ 派工（agent_id + 任務）
            ▼
   ┌──────────────────────┐
   │  8 個專責 Agent       │  ← 每個 Agent 只能用自己的工具白名單
   └──────────┬───────────┘
              │ tool call
              ▼
   ┌──────────────────────┐
   │  A：Tool Gateway      │  ← 統一入口：檢查權限 → 寫 log(C) → 執行 / 攔截送審批
   └──────────────────────┘
```

**依賴關係**（摘自執行手冊 2.2）：
- B 的派工邏輯依賴 **A 的工具分級** → 本文件的工具白名單已對齊 A 的 `tool_classification.py` 的 `module` 欄位。
- B 的派工結果要記錄到 **C 的 `agent_action_logs`** → 見 §4 派工紀錄欄位建議。

---

## 1. Agent 名單（8 個 = 7 個專責 + 1 個客服 fallback）

| # | agent_id | 中文名 | 對應 A 的 module | 工具數 | 可寫入 | 職責一句話 |
|---|----------|--------|------------------|:---:|:---:|------------|
| 1 | `inventory_agent`   | 庫存 Agent       | `inventory` + `manufacturing` | 9 | ✅ | 庫存/缺貨/補貨/成本，及製造 BOM、工單 |
| 2 | `procurement_agent` | 採購 Agent       | `procurement` | 3 | — | 供應商、採購單、應付帳款 |
| 3 | `sales_agent`       | 銷售 Agent       | `orders` | 5 | ✅ | 訂單建立/查詢、應收、客戶、報價 |
| 4 | `finance_agent`     | 財務 Agent       | `finance` | 3 | — | 總帳、財務概況、公式計算 |
| 5 | `hr_agent`          | 人資 Agent       | `hr` | 3 | — | 員工資訊、薪資、出勤（敏感） |
| 6 | `esg_agent`         | ESG Agent        | `carbon` | 4 | — | 碳排、碳足跡、ESG 目標 |
| 7 | `risk_agent`        | 供應鏈風險 Agent | `ai_supply_chain` | 3 | — | 風險事件、受影響採購單、風險熱圖 |
| 8 | `cs_agent`          | 客服 Agent       | （無專屬工具）| 0 | — | 招呼、一般問答、跨領域早報彙整、**fallback** |

> **製造工具的歸屬說明**：手冊列的 7 個專責 Agent 沒有獨立「製造 Agent」，故將 `manufacturing`
> 的 `get_bom_list`、`get_work_orders_status` 併入庫存 Agent（A 的分級裡這兩個也屬 `warehouse` 角色）。
>
> **「可寫入」說明**：標 ✅ 的 Agent 白名單裡含 `write` 級工具（`update_inventory` / `create_order`）。
> 「可寫入」**不代表直接執行** — 依手冊底線，所有 write 操作一律先進 C 的 `pending_approvals` 等待人工審批。

### 1.1 每個 Agent 的工具白名單（30 個工具全覆蓋、無重複）

| Agent | 工具白名單 |
|-------|-----------|
| 庫存 | `check_inventory`, `get_all_inventory`, `get_low_stock_inventory`, `get_inventory_total_value`, `get_cost_analysis`, `calculate_smart_restocking`, **`update_inventory`** (write), `get_bom_list`, `get_work_orders_status` |
| 採購 | `get_payables`, `get_suppliers_list`, `get_purchase_orders_summary` |
| 銷售 | `get_recent_orders`, **`create_order`** (write), `get_receivables`, `get_customers_list`, `get_quotations_summary` |
| 財務 | `get_ledger_summary`, `get_financial_overview`, `calculate` |
| 人資 | `get_employee_info`, `get_payroll_summary`, `get_attendance_summary` |
| ESG | `get_carbon_emissions_by_month`, `get_carbon_emissions_by_year`, `get_carbon_footprint_report`, `get_esg_targets` |
| 風險 | `get_supply_chain_risk_events`, `get_impacted_purchase_orders`, `get_supply_chain_heatmap_summary` |
| 客服 | （無；必要時由總管改派或彙整其他 Agent 結果） |

> 此白名單即 A 在 Day 2 要綁定的「每個 Agent 的工具白名單」之 B 方草案。程式中由
> `get_tools_for_agent(agent_id)` 提供：**A 的 `tool_registry` 合併後會自動改用
> `registry.get_tools_by_module()`**（單一真實來源），合併前則用本檔內建白名單。

---

## 2. 路由規則表（任務分類 → 派給哪個 Agent）

總管收到任務後，依「意圖／關鍵詞」判斷主責 Agent。下表為**判斷準則**；Day 1 先用關鍵字版
`route_by_keyword()` 驗證，Day 2 改由 Gemini 依下表語意判斷。

| 主責 Agent | 典型問句 / 意圖 | 觸發關鍵詞（部分） |
|-----------|----------------|-------------------|
| 庫存 | 「哪些商品快缺貨」「幫我補貨」「庫存價值多少」「這顆成品的 BOM」 | 庫存, 缺貨, 補貨, 安全庫存, 進貨, 成本, 毛利, BOM, 工單, 製造 |
| 採購 | 「供應商名單」「未結採購單」「應付帳款多少」 | 採購, 供應商, 採購單, 應付, PO |
| 銷售 | 「幫客戶建一張訂單」「本月業績」「應收帳款」「報價單」 | 銷售, 訂單, 下單, 出貨, 客戶, 報價, 應收, 業績 |
| 財務 | 「總帳摘要」「財務概況」「幫我算這筆」 | 財務, 總帳, 損益, 財報, 營收, 計算, 試算 |
| 人資 | 「某員工資料」「這個月薪資」「出勤統計」 | 員工, 人資, 薪資, 出勤, 請假, 人事 |
| ESG | 「碳排放多少」「碳足跡報告」「減碳目標」 | 碳排, 碳, ESG, 減碳, 碳足跡, 永續, 排放 |
| 風險 | 「供應鏈有什麼風險」「戰爭影響哪些採購單」「風險熱圖」 | 風險, 供應鏈, 斷鏈, 延遲, 熱圖, 戰爭, 罷工, 天災, 新聞 |
| 客服 | 招呼、操作說明、無法歸類、空泛問題 | 你好, 嗨, 謝謝, 怎麼用, 說明, 幫助 |

### 2.1 路由決策規則（總管要遵守的優先序）

1. **單一意圖** → 派給該主責 Agent。
2. **跨領域 / 早報型**（如「今天營運狀況如何」「老闆早報」）→ 總管**依序**派給多個 Agent
   （庫存→銷售→財務→風險），各自回結果後由**客服 Agent / 總管彙整**成一則回覆。見 Demo 3。
3. **完整補貨情境**（低庫存 → 補貨建議 → 採購）→ 主責庫存 Agent，必要時**接力**採購 Agent。見 Demo 1。
4. **歧義 / 無法判斷** → 派 `cs_agent`（fallback），由客服澄清或請使用者補充。
5. **權限不足**：若該 Agent 要用的工具被 A 的 Gateway 依使用者角色擋下，總管須回覆友善說明，不可硬闖。

### 2.2 關鍵字路由的已知極限（為何 Day 2 要換 LLM）

Day 1 的 `route_by_keyword()` 是「數關鍵字命中數」的簡易版，已驗證可跑，但有兩類它處理不好的情況，
正是 Day 2 改用 Gemini 語意路由的理由：

- **多領域歧義**：「台灣供應商有沒有受**戰爭**影響的**採購單**」→ 關鍵字版命中「供應商+採購」(2) >「戰爭」(1)
  而誤判為採購；語意上應為**風險 Agent**。
- **同義詞 / 口語**：「幫我**算** 3500×12」未命中財務關鍵詞「計算」而落到 fallback；LLM 能理解這是試算意圖 → 財務。

---

## 3. 總管 Agent system prompt 草稿

> 這是 Day 2 `agent_orchestrator.py` 的起手 prompt。實作時把 §1/§2 的名單與規則注入
> `{agent_catalog}`，並要求 Gemini 以固定 JSON 輸出，方便程式解析與寫 log。

> **Day 2 升級方向**：
> 1. **六段式 system prompt**：把本草稿整理成 `Role / Objective / Tools / Examples / Schema / Instructions` 結構。
> 2. **`<agents>` 標籤注入**：Agent 名單改用 `<agents>...</agents>` XML 標籤，未來可在每個 Agent 內巢狀塞其工具 `<tools>`。
> 3. **LLM Router**：路由主力改由 Gemini 語意判斷（單一任務走 Router、跨領域早報走 Supervisor），`route_by_keyword()` 降為 fallback。建議路由用 Flash、執行用 Pro。
> 4. **few-shot 內嵌**：把 §3.1 的範例直接寫進 prompt 本體。
> 5. **維持強制 JSON 輸出**（本草稿已採用的 Structured Output 模式）。

```text
你是進銷存 ERP 系統的「總管 Agent（Orchestrator）」。
你本身不直接查資料、不呼叫工具，你的唯一職責是：讀懂使用者的任務，判斷該交給哪一個專責 Agent 處理，並在需要時把多個 Agent 的結果彙整成一則回覆。

【你管理的專責 Agent 名單】
{agent_catalog}
（每筆含：agent_id、中文名、職責、可用工具範圍）

【你的判斷規則】
1. 先判斷任務屬於哪一個領域，選出「主責 Agent」。
2. 若任務同時牽涉多個領域，或屬於「營運總覽 / 老闆早報」類，請列出需依序執行的多個 Agent，最後指定由 cs_agent 彙整。
3. 若任務需要寫入資料（建立訂單、更新庫存等），你只負責「派工」，實際寫入會由 Tool Gateway 攔截並送人工審批，你不可宣稱已完成。
4. 完全無法歸類、或只是招呼／閒聊，派給 cs_agent。
5. 你只能從上方名單挑 agent_id，絕對不可自創不存在的 Agent。

【輸出格式】請只輸出 JSON，不要多餘文字：
{
  "task_type": "<single | multi | smalltalk>",
  "primary_agent": "<agent_id>",
  "agent_chain": ["<agent_id>", ...],   // 單一任務時與 primary_agent 相同；多領域時依執行順序列出
  "needs_approval": <true | false>,      // 任務是否預期會觸發寫入型工具
  "reason": "<一句話說明你為什麼這樣派>"
}
```

### 3.1 輸出範例

| 使用者任務 | 期望輸出（重點欄位） |
|-----------|---------------------|
| 「哪些商品快缺貨了？」 | `primary_agent=inventory_agent, task_type=single, needs_approval=false` |
| 「幫客戶王小明下一張 10 台筆電的訂單」 | `primary_agent=sales_agent, needs_approval=true`（create_order） |
| 「老闆早報：今天營運狀況」 | `task_type=multi, agent_chain=[inventory_agent, sales_agent, finance_agent, risk_agent], primary=cs_agent` |
| 「台灣供應商受戰爭影響的採購單有哪些」 | `primary_agent=risk_agent, task_type=single` |
| 「你好」 | `task_type=smalltalk, primary_agent=cs_agent` |

---

## 4. 與 C（審批稽核）的介面對齊（Day 1 下午要談）

派工結果要能寫進 C 的 `agent_action_logs`。B 建議在「派工」這一層至少記錄：

| 欄位 | 來源 | 說明 |
|------|------|------|
| `task` | 使用者輸入 | 原始任務文字 |
| `primary_agent` | 總管輸出 | 主責 Agent |
| `agent_chain` | 總管輸出 | 多領域時的執行序列（JSON 字串） |
| `routed_by` | 系統 | `llm` 或 `keyword`（fallback 時） |
| `needs_approval` | 總管輸出 | 是否預期觸發寫入 |
| `created_at` | 系統 | 派工時間 |

> 工具層的呼叫紀錄（工具名、參數、成功/失敗）由 A 的 Gateway 觸發、寫進 C 的 log，B 不重複記。
> B 這層記的是「**派工決策**」本身，供 D 的 Dashboard 顯示「哪個任務派給了哪個 Agent」。

---

## 5. 交付確認（對照手冊 Day 1 上午）

- [x] 確定 8 個 Agent（7 專責 + 客服）→ §1
- [x] 定義任務分類規則（什麼問題派給誰）→ §2
- [x] 總管 Agent system prompt 草稿 → §3
- [x] 工具白名單對齊 A 的分級（30/30 全覆蓋、無重複、無遺漏）→ §1.1（已用程式驗證）
- [x] 程式雛型 `backend/agent_registry.py`，含 `route_by_keyword()` 可即時驗證

**下一步（Day 1 下午／晚上）**：與 A 對齊工具綁定、與 C 對齊派工 log 欄位（§4）。
**Day 2 上午**：把本檔升級成 `agent_orchestrator.py`，用 §3 的 prompt 接 Gemini 做語意路由。
