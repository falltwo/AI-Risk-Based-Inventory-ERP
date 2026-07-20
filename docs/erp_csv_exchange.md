# ERP CSV 批次交換（L3 V1）

此功能是固定資料契約的批次交換原型，不是即時 ERP API 連接器，也不是通用 ETL。

- 一列代表一張採購單，且每張單只有一個品項。
- `source_system + external_id` 是外部資料身分；相同內容重匯不新增，內容改變才升版。
- 上傳只預覽；寫入暫存區、送審、管理員核准、動作檔匯出與回執對帳是不同狀態。
- 已核准動作從交易內的 execution receipt 快照匯出，不從可變暫存資料重建。
- 回執必須有 HMAC-SHA256 簽章；人工自行填入 `accepted` 不會被接受。
- 舊版未簽章回執只保留為 `unverified_legacy` 歷史，不會顯示成 ERP 已驗證。

## 啟用回執驗證

應用程式與外部 ERP 連接器需透過安全的部署設定取得相同金鑰；不要把金鑰寫入 repo 或 CSV。

```powershell
$env:ERP_EXCHANGE_RECEIPT_KEY_ID='demo-connector-v1'
$env:ERP_EXCHANGE_RECEIPT_HMAC_SECRET='<至少 32 bytes 的隨機秘密>'
streamlit run app.py
```

未設定或密鑰少於 32 bytes 時，系統仍可匯入、送審與匯出動作檔，但回執範本／對帳會 fail closed。

## Demo 外部連接器

1. 在「採購管理 → ERP CSV 交換 → ERP 回執對帳」下載回執範本。
2. 在受控的外部 ERP／模擬器環境設定同一組 key ID 與 secret。
3. 產生帶獨立 attempt ID 的簽章回執：

```powershell
python scripts/sign_erp_receipt.py receipt_template.csv signed_receipt.csv --status accepted --message "ERP import OK"
```

4. 將 `signed_receipt.csv` 上傳對帳。同一 attempt 重送是冪等；`error`／`rejected` 可用新的 attempt 重試，已驗證的 `accepted` 不會被後續失敗回執降級。

正式部署時，簽章程式與 HMAC secret 應位於 ERP 端或受控整合服務，不應交給一般 Web 操作員。

V1 只支援單一 active key。金鑰輪替與舊 key 驗證窗口尚未實作；輪替前須先完成 keyring／版本化驗證設計。若 Web 主機或 HMAC secret 整體失陷，回執來源保證也會失效。
