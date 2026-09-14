# PR #16 + PR #17 整合交付報告

日期：2026-09-14  
整合分支：`codex/integrate-pr16-pr17`  
本機程式提交：`2932e1a0f524967ba91c346ce5fea0848f231ad1`、`253c01d322d5b9a58c87f65bef37011329b9f7f3`  
PR #16 基準：`7538a410b7d14e6f88e6f9ac98d789109e014576`  
PR #17 基準：`0bc642f9468a50567eb4f765534236cee155e8af`

## 做了什麼

這次以 #16 的新聞資料、分析狀態、去重、排程、地區規則及熱圖保存為底，再把 #17 的 L1、L2、L3 功能搬入同一個 worktree。沒有整份採用任一 PR，也沒有改主工作區、推送遠端、更新 PR、部署或啟動正式服務。

### #16 的規則保留

- 原始新聞保留在原始欄位；AI 結果寫入 `analysis_status`、`analysis_error`、`analysis_country`、`analysis_region`、`analysis_summary` 等欄位。
- `succeeded`、`failed`、`pending`、`legacy_unverified` 分開處理；失敗不會自動變成 7 天風險。
- URL/content hash 及資料庫唯一索引負責去重；成功分析不重做，失敗與待處理資料可再次分析。
- 排程維持明確觸發、重試及跨程序工作鎖；背景排程預設關閉。
- `backend/region_matching.py` 成為 Python 與 SQLite 的地區匹配入口，國家與分區同時存在時使用交集。
- 熱圖的風險、延遲及摘要使用同一筆套用交易；0% 與 0 天保留為有效值，未知值保留為 `None`。
- 隔離執行器固定使用 fixture、關閉外部網路、移除 API 金鑰及正式通知設定。

### #17 的功能整合

- L1 直接讀資料庫顯示已確認事件與 AI 待確認告警，保存已讀、處理中及通知 L2 狀態。
- L1 可將系統內未結採購單對映到風險事件，無須上傳 CSV。
- L2 熱圖分數加入事件嚴重度、事件數量、時效及可讀依據；曝險金額改讀未結採購單並顯示供應商數。
- L2 摘要、更新建議、事件建議及來源資料落地到 `risk_ai_summaries`，換頁或重整後可重新載入。
- L2 可標記受影響採購單、建立替代供應商提案；L3 可查看事件與新聞證據、核准或駁回，結果回到 L1 的提案計數。
- 事件 identity 保留事件類型，因此同一地點的罷工與地震不會互相覆蓋。
- LLM 額外 headers、timeout 及 demo seed 設定保留，但隔離測試預設關閉 demo seed。

## 衝突取捨與相容性修補

### 分析與來源

新增 `backend/risk_contract.py` 統一新聞有效性與事件欄位驗證。L1、L2、L3 不再用原始新聞的國家、地區或摘要取代分析欄位；新聞來源若不是成功、相關且延遲已知，不能建立新聞事件，也不會出現在有效告警或摘要證據中。

### 證據與摘要

新增 `backend/risk_intelligence.py`。證據只收成功且相關的新聞，地點以國家/分區組合保存，來源 ID、分析狀態、摘要與時間一起保存。沒有有效證據時，AI 更新與事件會被略過；未知延遲不能被當作 0 或 7 天。失敗摘要不會取代資料庫內最新成功摘要。

### 地區

事件、L1 告警、L2 證據、熱圖節點、供應商曝險及採購對映均改用共用 resolver。這修正了 #17 原本只比國家、雙向 substring 以及未處理「臺灣／台灣」別名造成的跨分區誤命中。

### 0、未知與 UI 保存

所有事件建立/更新 API 使用嚴格數字驗證。地圖新聞捷徑、批次登錄、熱圖編輯器與手動登錄都保留 0；未知值不會被補成 7。AI 摘要失敗時畫面保留上一個有效摘要並顯示失敗狀態。

### 事件與權限

採用 #17 的事件類型 identity，再接回 #16 的來源、型別、範圍及地區驗證。planner 的 `RISK_WORKSPACE_WRITE` 只能修改採購單的風險註記欄位，不會核准採購或改供應商/金額/交易狀態。L3 的提案核准仍須 approver 權限。

### 沖銷

新增 `backend/approval_reversal.py`。沖銷現在以 `BEGIN IMMEDIATE`、`approval_reversals` 唯一鍵、原始執行收據及同一交易保護訂單/庫存/異動紀錄/稽核寫入。同時請求只會有一筆有效沖銷；舊審批若沒有唯一執行收據會拒絕自動處理，交由人工對帳。Gateway 也不再接受未綁定審批的直接 `rollback_inventory`/`cancel_order`。

## 測試

### 已通過

- 完整 pytest：**504 passed in 36.69s**。
- `pip check`：`No broken requirements found`。
- `git diff --check`：通過。
- 兩個 PR 的相關 L1/L2/L3、授權、排程、資料管線及 UI 測試均在同一份整合工作樹執行。
- 新增 `tests/test_pr16_pr17_integration.py`，涵蓋：
  - L1 → L2 通知 → 事件登錄 → L3 提案/證據 → L1 提案狀態回讀。
  - 成功、失敗、legacy、0、未知、非法天數及重複事件。
  - 共用地區匹配、別名、分區隔離及曝險金額。
  - 摘要 provenance、失敗摘要不覆蓋成功摘要、熱圖交易回滾。
  - 空資料庫/舊資料庫升級與重複初始化。
  - 兩個子程序同時沖銷只產生一次庫存異動。
  - Streamlit L1/L2/L3 主要元件重整後仍讀到資料。
- 隔離單次排程：

  ```text
  fetched_count=6, saved_count=6, analyzed_count=6,
  failed_count=0, pending_count=0, heatmap_status=succeeded
  ```

- 同一 `job_key=integration-review-v1` 再執行回傳 `status=skipped`。
- 啟動隔離 Streamlit 後 `http://127.0.0.1:8513/_stcore/health` 回傳 HTTP 200，之後已停止。

### 尚未驗證

- 沒有使用真實 GNews、Gemini 或其他付費模型；此次網路被封鎖，6 則是既有固定 fixture。
- 沒有發送 LINE、Email 或其他真實通知；L1 通知是資料庫內狀態轉移。
- 沒有在正式資料庫、正式排程器或生產多組織環境執行。
- 沒有做瀏覽器人工逐頁操作錄影；Streamlit `AppTest` 已涵蓋主要元件流程。
- 舊資料的自動分析回補策略尚未決定；目前維持 `legacy_unverified`，不自動升格為有效分析。

## 隔離環境

整合 worktree：

```text
C:\新EPR系統\ERP-pr16-pr17-isolated
```

分支：`codex/integrate-pr16-pr17`  
測試資料庫（已被 `.gitignore` 排除）：

```text
C:\新EPR系統\ERP-pr16-pr17-isolated\.isolated\erp-batch1-success.db
```

啟動 Streamlit 預覽（固定新聞、模擬 LLM、禁止外網、背景排程關閉）：

```powershell
Set-Location 'C:\新EPR系統\ERP-pr16-pr17-isolated'
& 'C:\新EPR系統\AI-Risk-Based-Inventory-ERP-new\.venv\Scripts\python.exe' scripts/run_isolated.py --scenario success --integration-demo --port 8513
```

單次排程驗收（必須明確指定工作鍵）：

```powershell
& 'C:\新EPR系統\AI-Risk-Based-Inventory-ERP-new\.venv\Scripts\python.exe' scripts/run_isolated.py --scenario success --integration-demo --scheduler-once integration-review-v1
```

停止預覽：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8513 -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty OwningProcess -Unique |
  Stop-Process -Force
```

`run_isolated.py` 會明確設定 `ERP_DB_PATH`、`ERP_SCHEDULER_ENABLED=0`、`ERP_ISOLATED_TEST=1`、`ERP_ENABLE_DEMO_SEED=0`，並在啟動時移除 GNews/LLM/通知金鑰。不要把 `.isolated` 內資料庫、XML、快照或任何 `.env` 加入 Git。

## 可審查差異與提交

第一個整合提交：

```text
2932e1a0f524967ba91c346ce5fea0848f231ad1
Integrate PR17 tiers with PR16 analysis, geography and persistence contracts
```

主要新增模組：

- `backend/risk_contract.py`：新聞/事件共用資料契約。
- `backend/risk_intelligence.py`：摘要 provenance、有效證據及安全閘門。
- `backend/approval_reversal.py`：審批綁定的 exactly-once 沖銷。
- `tests/test_pr16_pr17_integration.py`：整合回歸測試。

可用下列命令檢查差異：

```powershell
Set-Location 'C:\新EPR系統\ERP-pr16-pr17-isolated'
git show --stat --oneline 2932e1a
git diff 7538a410..2932e1a -- backend frontend tests scripts
git status --short
```

第二個本機提交只包含本報告；沒有遠端提交或 PR 更新。

第三個本機提交 `253c01d` 加入整合回歸測試及最後的嚴格事件來源/天數過濾。

第四個本機提交 `3985a17` 只修正本報告中的完整提交清單。

## 剩餘問題與建議方向

整合前必須由審查者確認：事件 identity 是否還需要「事件批次/episode」欄位、legacy 資料要採人工重審還是離線回補、以及組織權限資料在正式部署的初始 migration。這三項會影響資料治理，不應在未決定前自動修改既有資料。

可留到下一階段的工作包括真實新聞供應商輪替、付費模型觀測與成本控管、外部通知傳送、更多瀏覽器端 UX、以及報表/效能優化。本次沒有開始這些功能。

目前整合分支停在本機，主工作區與兩個原始 PR 分支都保留，等待你檢查提交及隔離畫面。
