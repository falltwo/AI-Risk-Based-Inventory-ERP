# 第一批改善 1～6：隔離實作與檢查

基準：`fcc2737`；分支：`codex/batch1-isolated`。

2026-09-14 更新：功能驗收提交 `b258d44` 的 Windows 完整測試為 **388 passed（27.41 秒）**；後續 GNews 真實擷取 6 篇、模擬 AI 流程驗收 14 項通過。以下 387 項與本機網站健康檢查是最初修補的歷史紀錄，並非網站目前仍啟動。這批成果以 Draft PR 供協作審查，暫不合併或部署。技術步驟見 [技術流程與 PR 操作說明](batch1-technical-workflow.md)。

## 環境與範圍

- Worktree：`C:\新EPR系統\ERP-batch1-isolated`。
- 主要工作區 `C:\新EPR系統\AI-Risk-Based-Inventory-ERP-new` 的程式、`.env`、資料庫及 6 個未追蹤架構圖片均未修改。
- 檢查資料庫：`C:\新EPR系統\ERP-batch1-isolated\.isolated\erp-batch1.db`。
- 啟動器在任何後端匯入之前明確設定 `ERP_DB_PATH`，不繼承外部資料庫路徑；成功案例使用另一個 `erp-batch1-success.db`。
- 重用現有 Python 3.12.3 虛擬環境的已安裝依賴，未修改依賴版本。亦可在 worktree 建立自己的 `.venv`，安裝 `requirements.txt` 與 `requirements-dev.txt`，啟動器會優先使用它。
- 新聞與 LLM 使用固定模擬資料；關閉背景排程、不載入原工作區 `.env`，移除程序內供應商金鑰及代理設定，阻擋 Python 程序的對外 DNS/TCP 連線；只允許本機連線。未啟動 LINE Bot。
- 未執行第二、三批功能；初次驗收未推送，後續以 Draft PR 協作審查，暫不合併或部署。

## 修改對照

| 項目 | 修改與行為 | 主要位置 |
|---|---|---|
| 1. AI 失敗不成為風險 | 移除例外時「相關、延遲 7 天」的預設；原始標題、本文、國家、URL 保留，分析摘要與地區存入獨立欄位；失敗及未驗證新聞不進入熱圖分析或事件登錄 | `risk_validation.py`、`news_store.py`、`supply_chain_news.py` |
| 2. 排程入口 | 明確啟用、間隔與重試次數可設定；SQLite 保存工作識別碼、狀態、嘗試次數；跨程序 OS 檔案鎖避免同時執行；成功識別碼跳過，失敗識別碼可重跑；程序終止後鎖由 OS 釋放 | `scheduler.py`、`job_lock.py`、`.env.example` |
| 3. 新聞去重 | 分析前以 URL 正規化雜湊、標題＋來源＋發布日雜湊尋找既有新聞；URL 去除追蹤參數與片段；資料庫唯一索引再防止重複寫入；成功新聞不重做逐篇分析，失敗／待分析新聞可重試 | `news_store.py`、`supply_chain_news.py` |
| 4. 嚴格 AI 驗證 | 檢查 JSON 結構、重複鍵、新聞編號、重複／遺漏結果、相關性、事件類型、文字、整數天數與數值範圍；拒絕布林、數字字串、NaN、Infinity、負數及越界值；零為有效值，null 為未知，例外為失敗 | `risk_validation.py`、`supply_chain_risk.py`、`prompts.py` |
| 5. 地區規則 | Python 與 SQLite 共用同一函式；國家＋子地區為交集，多選為聯集，廣域名稱展開固定國家表；支援臺灣／台灣、韓國／南韓、阿聯酋及常見英文別名；不使用子字串或 SQL 萬用字元 | `region_matching.py`、`supply_chain_risk.py`、`supply_map.py` |
| 6. 畫面與保存一致 | 熱圖新增持久化延遲欄位；審核表與寫入使用同一地區解析；單一交易保存所有選取節點的百分比與天數，失敗整批回滾；0%／0 天可保存，空白天數為未知；重新開頁讀取資料庫值 | `supply_chain_risk.py`、`supply_map.py`、`risk_dashboard.py` |

新聞刷新保留既有的自動更新熱圖行為，但僅使用驗證成功且相關的新聞。熱圖分析本身失敗時不覆寫既有值，排程收到可重試的失敗狀態。熱圖建議的保存不等於登錄正式事件，畫面成功訊息已說明此區別。手動登錄未有 AI 天數的事件須在畫面確認天數，不再靜默補入 7 天。

分析狀態：`pending` 待分析、`succeeded` 成功（可包含未知天數）、`failed` 失敗、`legacy_unverified` 舊資料待確認。錯誤欄位保存錯誤代碼，不將供應商例外內容寫入原始新聞。

資料表升級可重複執行。既有新聞與事件 ID 保留，不刪除歷史重複新聞；只有第一筆取得唯一識別鍵。舊新聞標記為未驗證，其來源事件不供本次風險計算使用。過去已被覆寫的原始摘要無法自動還原，既有熱圖覆寫值也未進行來源推測或清除；正式資料盤點不在這次隔離執行內。

## 啟動檢查

PowerShell：

```powershell
Set-Location 'C:\新EPR系統\ERP-batch1-isolated'
.\scripts\start-isolated.ps1
```

開啟 <http://127.0.0.1:8511>。測試帳號／密碼為 `planner`／`planner`；管理員為 `admin`／`admin`，僅存在隔離 Demo 資料庫。

啟動器預設展示台灣北區、台灣南區、日本東京 3 個正式測試據點。進入供應鏈風險頁面：

1. 「更新即時新聞」會產生確認零延遲、5 天延遲、未知天數、格式錯誤四類固定資料。
2. 重按刷新：原始新聞筆數不增加；格式錯誤會重試並保持失敗狀態。
3. 檢查失敗新聞原文仍在，事件登錄停用；未知與 0 天的顯示不同。
4. 「產生／更新即時風險摘要」會得到台灣北區 0%／0 天、日本 65%／5 天建議。編輯並套用，重新開頁檢查保存結果。
5. 台灣北區的事件與採購／缺貨分析不應包含台灣南區或日本北區。

預設不啟動任何背景排程。以下是**手動一次性**排程測試，固定識別碼重跑可檢查防重複執行：

```powershell
$py = 'C:\新EPR系統\AI-Risk-Based-Inventory-ERP-new\.venv\Scripts\python.exe'
& $py scripts/run_isolated.py --scenario success --scheduler-once review-success
& $py scripts/run_isolated.py --scenario success --scheduler-once review-success
# 第一次成功；第二次 skipped。資料庫為 .isolated/erp-batch1-success.db。

& $py scripts/run_isolated.py --scheduler-once review-failure
# 固定格式錯誤案例：重試後 failed，退出碼 1；失敗資料仍可檢查。
```

通用入口為 `python -m backend.scheduler --once --job-key <識別碼>`，要求明確 `ERP_DB_PATH` 與有 `risk.workspace.write` 權限的 `ERP_SCHEDULER_ACTOR`。背景入口 `start_background_jobs()` 額外要求 `ERP_SCHEDULER_ENABLED=1`；本次未掛入 app 啟動，也未啟動它。設定預設為 86400 秒間隔、10 秒起始延遲、3 次嘗試、30 秒重試等待。`ERP_ISOLATED_TEST=1` 會強制停用背景入口。

鎖限定使用同一個本機 SQLite 路徑的程序。這次未實作跨機器或網路檔案系統上的分散式排程。

若此次協作啟動的 8511 程序仍在運行，可直接開啟網址，無須再啟動一份。其 PID 與輸出記錄在 `.isolated/server.pid`、`.isolated/server.stdout.log`、`.isolated/server.stderr.log`；重新啟動時可用 `-Port 8512` 指定另一個本機埠。

## 測試與審查

最終本機結果：**387 項通過，0 項失敗（27.33 秒）**，包含 60 個新增測試案例。JUnit 報告在 `.isolated/test-results.xml`。`git diff --check` 通過；本機 `http://127.0.0.1:8511/_stcore/health` 回傳 `ok`。

```powershell
Set-Location 'C:\新EPR系統\ERP-batch1-isolated'
$py = 'C:\新EPR系統\AI-Risk-Based-Inventory-ERP-new\.venv\Scripts\python.exe'
& $py -m pytest -q --disable-warnings
git diff fcc2737 --stat
git diff fcc2737 -- backend frontend tests scripts docs/batch1-isolated-review.md
```

pytest 在載入後端前將 `ERP_DB_PATH` 指向獨立暫存資料庫，且阻擋對外連線。測試涵蓋：原始內容保留、失敗不生風險、零／未知、嚴格 AI 驗證、去重、失敗回補、舊資料遷移、Python／SQL 地區一致性、跨程序鎖及程序中止後復原、重試、識別碼冪等、權限撤銷、寫入失敗回滾，以及 Streamlit 真實按鈕流程與新 session 讀取。

本機測試平台為 Windows／Python 3.12.3。POSIX 檔案鎖分支未在本機執行。

初次驗收未包含真實新聞服務；後續已分別完成 RSS 與 GNews 擷取驗收，AI 仍為模擬回應。尚未驗證：付費模型品質、真實 LINE 通知、正式資料庫遷移、大量資料效能及長時間背景運行。瀏覽器地圖底圖的外部素材可用性也不屬於本次後端連線驗證。
