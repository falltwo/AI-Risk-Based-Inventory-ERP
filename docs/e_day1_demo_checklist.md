# Day 1 Demo 功能交付與操作說明

本文件說明 Day 1 已完成的功能、使用方式、驗收方式，以及提交時應該包含哪些檔案。

## 已完成功能

### 1. LINE Bot 環境檢查

新增 `scripts/check_line_env.py`，用來確認 LINE Bot 啟動前需要的環境是否齊全。

這個檢查工具會確認：

- `.env` 是否存在。
- `LINE_CHANNEL_ACCESS_TOKEN` 是否有填。
- `LINE_CHANNEL_SECRET` 是否有填。
- `GEMINI_API_KEY` 是否有填。
- `fastapi`、`line-bot-sdk`、`python-dotenv`、`google-genai` 是否已安裝。

執行方式：

```powershell
python scripts/check_line_env.py
```

成功時會看到：

```text
LINE Bot readiness: READY
```

如果出現 `MISSING`，代表還缺少環境變數或套件。常見修正方式：

```powershell
pip install -r requirements.txt
```

或檢查 `.env` 是否填入正確金鑰。

### 2. `.env` 範本

新增 `.env.example`，讓隊員知道本機 `.env` 需要哪些欄位。

範本內容包含：

```text
LINE_CHANNEL_ACCESS_TOKEN=replace_with_line_channel_access_token
LINE_CHANNEL_SECRET=replace_with_line_channel_secret
GEMINI_API_KEY=replace_with_gemini_api_key
```

使用方式：

```powershell
Copy-Item .env.example .env
```

接著打開 `.env`，把範本值改成真實金鑰。

注意：`.env` 放的是真實金鑰，不要提交到 GitHub。

### 3. Demo 測試資料產生器

新增 `scripts/seed_e_day1_demo_data.py`，用來建立固定的 Demo 測試資料。

這個 script 會準備：

- 低庫存商品：`P001`、`P004`、`P019`
- 供應鏈風險事件：`亞洲 / 日本`，預估延遲 12 天
- 受影響採購單：`PO-E-DAY1-JP-001`
- 受影響銷售單：`ORD-E-DAY1-JP-001`、`ORD-E-DAY1-JP-002`
- LINE 測試紀錄使用者：`E_DAY1_DEMO_USER`

正式寫入本機資料庫：

```powershell
python scripts/seed_e_day1_demo_data.py
```

只測試、不寫入資料庫：

```powershell
python scripts/seed_e_day1_demo_data.py --dry-run
```

成功時會看到類似：

```text
E Day1 demo seed summary
Low-stock products:
- P001 高階筆記型電腦
- P004 螢幕顯示器
- P019 Type-C 轉接頭
Risk event: 港口壅塞 亞洲/日本 impact_days=12
Purchase order: PO-E-DAY1-JP-001
Affected sales orders:
- ORD-E-DAY1-JP-001
- ORD-E-DAY1-JP-002
```

這個 script 可以重複執行，不會一直新增重複資料。

### 4. LINE Bot 必要套件

已在 `requirements.txt` 補上 LINE Bot 需要的套件：

```text
fastapi==0.136.3
line-bot-sdk==3.23.0
uvicorn==0.48.0
```

安裝方式：

```powershell
pip install -r requirements.txt
```

## 操作流程

### 第一步：安裝套件

```powershell
pip install -r requirements.txt
```

### 第二步：建立 `.env`

```powershell
Copy-Item .env.example .env
```

填入真實金鑰：

```text
LINE_CHANNEL_ACCESS_TOKEN=你的_LINE_CHANNEL_ACCESS_TOKEN
LINE_CHANNEL_SECRET=你的_LINE_CHANNEL_SECRET
GEMINI_API_KEY=你的_GEMINI_API_KEY
```

### 第三步：檢查 LINE Bot 環境

```powershell
python scripts/check_line_env.py
```

預期結果：

```text
LINE Bot readiness: READY
```

### 第四步：建立 Demo 測試資料

```powershell
python scripts/seed_e_day1_demo_data.py
```

如果只是想確認 script 可以正常跑，但不想修改資料庫：

```powershell
python scripts/seed_e_day1_demo_data.py --dry-run
```

### 第五步：啟動 LINE Bot

```powershell
python "line bot/bot_server.py"
```

如果可以正常啟動，代表 LINE Bot 已能從 `.env` 讀取金鑰。

要停止服務時，在終端機按：

```text
Ctrl + C
```

## Demo 驗收方式

### 低庫存情境

可以在 LINE 詢問：

```text
請給我低庫存與 AI 補貨建議
```

預期應看到低庫存商品包含：

- `P001`
- `P004`
- `P019`

### 供應鏈風險情境

可以在 LINE 詢問：

```text
日本供應鏈風險會影響哪些訂單?
```

預期應能看到：

- 日本 / 亞洲風險情境
- 受影響採購單 `PO-E-DAY1-JP-001`
- 受影響銷售單 `ORD-E-DAY1-JP-001`、`ORD-E-DAY1-JP-002`

## 建議提交的檔案

請只提交以下檔案：

```powershell
git add requirements.txt .env.example scripts/seed_e_day1_demo_data.py scripts/check_line_env.py docs/e_day1_demo_checklist.md
```

這些檔案代表本次功能交付：

- `requirements.txt`
- `.env.example`
- `scripts/seed_e_day1_demo_data.py`
- `scripts/check_line_env.py`
- `docs/e_day1_demo_checklist.md`

## 不要提交的檔案

不要提交：

- `data/erp.db`
  - 本機 SQLite 資料庫，跑 seed script 後會改變。
  - 不建議推上 GitHub，避免覆蓋其他人的資料或產生二進位檔衝突。

- `.env`
  - 內含真實 LINE / Gemini 金鑰。
  - `.gitignore` 已忽略此檔。

- `.vscode/`
  - 本機 VS Code 設定，和功能交付無關。

提交前請檢查：

```powershell
git status --short
```

確認 `data/erp.db`、`.env`、`.vscode/` 沒有被 staged。

## 完成標準

符合以下條件即可視為 Day 1 功能完成：

- `python scripts/check_line_env.py` 顯示 `LINE Bot readiness: READY`。
- `python scripts/seed_e_day1_demo_data.py --dry-run` 可以正常跑完。
- LINE Bot 可以啟動並完成測試。
- Demo 低庫存資料可查到 `P001`、`P004`、`P019`。
- Demo 供應鏈風險資料可查到日本風險情境與受影響訂單。
- 沒有提交 `.env`。
- 沒有提交 `data/erp.db`。

