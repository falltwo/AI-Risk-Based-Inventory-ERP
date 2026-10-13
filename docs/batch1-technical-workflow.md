# 第一階段技術流程與 Draft PR 操作說明

本文件說明本批修補的執行流程、隔離方式、驗證證據，以及上傳 Draft PR 各步驟的用途。白話範圍說明見 [修改六項供應鏈區塊風險](修改六項供應鏈區塊風險.md)。

## 一、程式如何處理新聞與風險

| 步驟 | 技術做法 | 用途 |
| --- | --- | --- |
| 1. 驗證操作者 | 刷新入口要求 `risk.workspace.write`，透過 `actor` 載入有效權限；排程使用 `ERP_SCHEDULER_ACTOR` | 避免沒有權限的呼叫開始讀取與寫入風險工作區。 |
| 2. 取得執行鎖 | 新聞流程取得資料庫路徑對應的 OS 檔案鎖；排程另有自己的鎖 | 避免同一資料庫的不同程序同時刷新；程序結束或崩潰後由 OS 釋放鎖。 |
| 3. 取得新聞 | 一般模式使用 GNews，有需要時使用 RSS 備援；隔離模式讀固定資料或快照 | 將外部來源擷取和可重播的驗收分開。GNews 的來源國別不直接等於事件影響國別。 |
| 4. 找出重複新聞 | 正規化 URL、移除追蹤參數，另以標題、來源、發布日建立識別雜湊，搭配資料庫唯一索引 | 先找既有資料再分析，減少重複寫入與逐篇 AI 分析。 |
| 5. 保存原始內容 | `news_store.store_raw()` 保存原始標題、摘要、URL 等欄位；新資料為 `pending` | AI 失敗或重試時仍能追溯來源，不以 AI 內容覆蓋原文。 |
| 6. 驗證 AI 輸出 | 檢查 JSON、新聞編號、缺漏或重複結果、類型、數值範圍；熱圖更新另驗證合法據點 | 明確區分零、未知、失敗，不以預設 7 天補齊錯誤結果。格式驗證不等於證據充分或 AI 判斷正確。 |
| 7. 保存分析狀態 | 分開保存 `analysis_status`、錯誤代碼、分析摘要與分析地區 | 原始內容與分析結果可分別檢視；失敗資料不冒充成功結果。 |
| 8. 更新風險 | 熱圖 AI 輸入限成功且相關新聞；新聞來源事件另檢查有效來源與已知天數；查詢共用地區匹配函式 | 避免失敗新聞與錯誤地區影響熱圖、供應商、採購與缺貨判斷。 |
| 9. 套用與保存 | 審核與寫入共用據點解析；一次交易保存所選熱圖節點的風險、摘要、延遲 | 0% 與 0 天能保存；中途失敗整批回滾；新 session 可讀回資料庫值。 |
| 10. 排程記錄與重試 | `scheduled_jobs` 保存工作識別碼、狀態、次數與結果；成功識別碼跳過，失敗可重試，權限失敗停止重試 | 讓更新可追蹤，且重跑不代表重複執行已成功的工作。 |

新聞分析狀態為 `pending`、`succeeded`、`failed`、`legacy_unverified`。這些狀態描述分析結果，不代表人工已確認或已核准，也不等於事件已讀／處理中狀態。

## 二、隔離環境如何保護主工作區

1. **Git worktree 與分支分開。** 第一階段位於 `ERP-batch1-isolated`，分支為 `codex/batch1-isolated`。worktree 共享 Git 物件與 refs，但有獨立檔案目錄與索引；fetch 更新 refs 不等於把程式合併到 main。
2. **Python 執行環境與資料庫分開管理。** 本機重用既有虛擬環境的 Python／套件，未藉此共用正式資料庫。虛擬環境主要隔離套件，真正的資料隔離由明確的 `ERP_DB_PATH` 決定。
3. **啟動時明確指定測試資料庫。** `run_isolated.py` 在載入後端前設定 `.isolated/` 下的資料庫路徑，不繼承外部正式資料庫設定。
4. **網站檢查使用模擬。** 啟動器設定 `ERP_ISOLATED_TEST=1`、移除程序內供應商金鑰與代理設定、封鎖外部 DNS/TCP、停用背景排程。允許本機連線供 Streamlit 檢視；這不是整台電腦或瀏覽器的網路防火牆。
5. **真實新聞擷取使用獨立入口。** `accept_live_news.py` 明確讀取所指定檔案的 `GNEWS_API_KEY`，一次取得六篇，再封鎖後續外部網路，用模擬 AI 驗收。每次建立新的資料夾與 `acceptance.db`。
6. **快照檢視另外保存操作結果。** 指定驗收資料夾啟動網站時，先以 `acceptance.db` 建立 `preview.db`，畫面操作不覆寫原驗收資料庫或 `news-capture.json`。

## 三、如何在本機重現

以下指令供組員選擇執行；建立 Draft PR 本身不會啟動這些網站或真實新聞擷取。

### 準備依賴與執行離線測試

在自己的專案工作目錄執行：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
& .\.venv\Scripts\python.exe -m pytest tests/ -q
```

用途：建立自己的套件環境、安裝相同依賴、驗證程式。`tests/conftest.py` 在後端載入前將 `ERP_DB_PATH` 指向暫存資料庫，並封鎖外部網路、停用背景排程。

### 檢視固定測試案例

```powershell
& .\.venv\Scripts\python.exe scripts/run_isolated.py --port 8511
```

用途：啟動本機隔離 Streamlit。開啟 `http://127.0.0.1:8511`，以測試帳號 `planner`／`planner` 檢查第一階段流程。帳號由隔離 Demo 資料建立，不能當成正式部署的帳號設定。

### 一次性 GNews 驗收（會使用新聞 API 配額）

```powershell
& .\.venv\Scripts\python.exe scripts/accept_live_news.py --source gnews --env-file 'C:\你的設定位置\gnews.env' --country 美國
```

設定檔只需 `GNEWS_API_KEY=你的金鑰`；不要提交金鑰。腳本輸出 `.isolated/live-news-時間戳/` 路徑，包含原始新聞快照、驗收資料庫與結果報告。抓取不足六篇或 API 失敗時，不代表驗收成功；應先檢查查詢、配額與回應。

### 檢視已抓取的快照

```powershell
& .\.venv\Scripts\python.exe scripts/run_isolated.py --port 8511 --acceptance-dir '.isolated/live-news-實際時間戳'
```

用途：在供應鏈區塊檢視新聞來源與驗收資料。按「重播本批真實新聞」讀既有快照，AI 仍為模擬，不重新抓取 GNews。

## 四、上傳 Draft PR 的步驟與用途

| 順序 | 操作 | 用途與影響 |
| --- | --- | --- |
| 1 | 查看 `git status`、`git remote -v`、目前分支與提交 | 確認將上傳的是第一階段分支，保留使用者目前文件名稱與其他工作區資料。 |
| 2 | `git fetch --no-tags origin main`，比對 `origin/main` 與第一階段 | 取得最新基準，確認 PR 差異範圍；不 checkout、merge 或 pull 到主工作區。 |
| 3 | 檢查差異、忽略規則與待推送提交內容 | 排除 `.env`、金鑰、`.isolated/`、資料庫與驗收快照。只看 `.gitignore` 不夠，已追蹤檔案和新增提交內容也需檢查。 |
| 4 | 核對本機驗證紀錄、GitHub Actions 設定 | PR 說明區分真實新聞與模擬 AI；目前工作流程在 PR 上執行 Ubuntu／Python 3.11 測試，沒有部署步驟。 |
| 5 | 將本次說明文件明確 `git add`，再 `git commit` | 把程式、範圍、驗收與限制一起交給審查者；本機 commit 尚未上傳遠端。 |
| 6 | `git push --set-upstream origin codex/batch1-isolated` | 將此分支上傳，設定追蹤分支；不更新 main，不使用 force push。 |
| 7 | 建立 GitHub PR，`base=main`、`head=codex/batch1-isolated`、`draft=true` | 提供可討論的程式差異並標示仍待整合。此環境未安裝 gh，使用 Git Credential Manager 既有登入，透過 GitHub REST API 建立；憑證只在程序記憶體使用，不輸出或寫入 PR。 |
| 8 | 讀回 PR 狀態、SHA 與 CI 結果 | 確認是 Draft、來源與目標正確、遠端內容等於本機提交。Draft 仍可能執行 CI；CI 通過不代表已部署或完成業務驗收。 |
| 9 | 等待組員 PR，做獨立副本整合測試 | 對照 L1～L3 的實際修改，保留失敗隔離、去重、地區匹配與零值保存，避免直接整檔覆蓋。 |
| 10 | 日後完成整合審查，再另行決定轉正式審查與合併 | 這次只建立 Draft PR。合併與部署需要後續決定；不設定自動合併。 |

## 五、已驗證的範圍與後續整合

- 功能驗收提交 `b258d44`：Windows／Python 3.12.3，完整測試 **388 passed in 27.41s**。這是既有本機執行紀錄，不是本文件編輯時重新跑出的數字。
- GNews：六篇真實來源資料，模擬 AI 驗收 **14 項通過**。原始快照、資料庫與細節報告留在本機 `.isolated/`，不放入 PR。
- 先前分別與 PR #12、#13、#14、#15 的固定版本整合測試：406、392、393、395 項通過。這不是四個 PR 一起合併的結果，也不涵蓋尚未取得的組員 L1～L3 成果。
- #12 與 #15 在 LINE 身分處理互有衝突；#13 與 #15 在 `backend/auth.py` 衝突，整合時需保留缺少有效身分即拒絕的授權邊界。
- 尚未完成真實 LLM 品質、正式資料遷移、真實通知、正式負載與長時間排程驗收。新聞格式合法不等於證據充分，分析成功不等於人工確認，新聞去重也不等於事件去重。

PR 需維持 Draft，待組員提供 PR 後確認共同基準與重疊功能，再規劃第二階段剩餘工作。
