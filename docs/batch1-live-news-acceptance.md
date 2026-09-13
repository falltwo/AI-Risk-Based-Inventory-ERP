# 第一階段追加驗收：6 筆真實 RSS 資料

抓取時間：2026-09-13 19:14（台灣時間）。原修補提交：`5c9aa36`。

**結果：14 項流程檢查通過；使用同一份擷取資料離線重播，14 項也全部通過。**

本次外部來源是 Google News RSS。使用者指出的設定是 `GEMINI_API_KEY`，它不是 GNews 新聞金鑰；本次沒有使用 GNews 或 Gemini API。模型回應均為驗收模擬，沒有付費模型呼叫、背景排程或真實通知。

## 取得的 6 筆資料

以下是 RSS 條目的中文主題摘要；來源標示取自 RSS 標題，時間是 RSS 標示時間，未獨立核對各出版者頁面。

| # | 主題／來源連結 | RSS 標示來源 | RSS 標示時間 |
|---|---|---|---|
| 1 | [伊朗衝突追蹤](https://news.google.com/rss/articles/CBMingFBVV95cUxQMWduRExjZXhMUjFuYjV4Zzc0LS11OHVKUEhXSlpUNUdFMm9MVGlJeFd5eGh1ZWpEWWhFNEdJX0tOVTlJbnZ5NmpBQ3dYeGNpSTBOSWtISUduak5WSHhfQUhQcHBKbVpLdG1IVi1ySWVGd05NM18yektrem1TM3ZvaV9xVjR2eC04cE9RZ3FZajNCZWFHNzBaS0hhSS02dw?oc=5) | Council on Foreign Relations | 2026-09-09 00:00 |
| 2 | [美國 2022 年降低通膨法政策資料](https://news.google.com/rss/articles/CBMiZkFVX3lxTE1WZ1JFc24yV0d0Z09fdUF1ZTluaWJVVmZwY3I3SnFjb01GT3dUbk5zemY1OFAzeUU5aFVNNHQ3MlZ0MVlPS0hSVGV5eFZJWGRTdmFpNS1ueUVsbjQzUVoxWjRycmo0UQ?oc=5) | 美國能源部 | 2026-09-10 16:21 |
| 3 | [九一一事件 25 年後的恐怖主義情勢分析](https://news.google.com/rss/articles/CBMijwFBVV95cUxPaEFiSFBMOTZIeFNqLUJVRzFYcHdaV3ZoTHZ4NldxTnY2NlJwa3BuMDNqV21PUHUyWjRLV1Q1UXFNbGZYYTBiRnNaSEF5WC1BNHBXYUtTTG9ocWFrTUVYcHZtUGc1NklSNWs5T3lMeF9iMTdWbzdZaEtoOFNmSXVDX2JIMnNVd3VQc1JYOGRyMA?oc=5) | Atlantic Council | 2026-09-10 10:00 |
| 4 | [美國宣稱摧毀五艘伊朗油輪的報導](https://news.google.com/rss/articles/CBMirgFBVV95cUxNeTFBdWRVb3JiRXFnODNwakw4d3IxdlNzZUFnSXhKN1BVVTc4VXlZMl9sYzJFUGtGWnRyVU9wSi1Ua1FoVUhtYVNaaDdhMG9MOE1nd1F3RHlVNHJWSlYxbTY4RVdTQTFpSEtJYnV3TmpxR2FsS0x4RGVOdjZMak1HLWN0YzVmamxyYkZncy1jOEpybGloT1NxVzlPZG5jUlFwY082VUtndEFVUnVHUkHSAcIBQVVfeXFMUFVIY0NEVmtnbUZPTWpkQXVTZER5UXZDa2VuaFQwWmlfOGFrcmI2TXJOSmdaVW4tcVdBUXpyRTVnX2l0dE5Rb2VhdUo0Wno4UUV3dXVKWkY4eF9WdVRrejk3OW1YaWpIM3NLUUxab2h3azBWd3dqblZpZDRzel9USUFiak1UbElOY3NyNWtZbEprZ0N5ZnpfQmJIVTc2R3FMOTVEWS1VWWUxanpqS0ptZzZOWHZTTHNER05mX216SjRHcHc?oc=5) | EL PAÍS | 2026-09-09 09:54 |
| 5 | [荷姆茲海峽重新通航進展與伊朗戰事的報導](https://news.google.com/rss/articles/CBMixwFBVV95cUxPVFowaUI1M1lFY0RPVk9JMjY0XzdTdkVoV0JxZzZjcEZlZHM2eWpCZXE2N3JMajhqWUl5RlpCOGlsYjlqeHlvczN5ZHAtUVVVVnE4djF2LVFqUkxYbnNSVmRtQ3N2Z1pDbFZYM0s0N213RkU2Zjh4elFjZVpiUFEwc0FXNFRyd1RYTXlFV1ZLc01WbXpVSkxHMmk2UFp6aWM1Tk4tMThWX1I1YUlyU0FzSGJvU2FyMGMyYUhrRE01MVVuanRZdTJr0gHMAUFVX3lxTE5DMm15WVlJdkwwYXVLcy1KdS1mdlVGMmRST2tNSldPOFZWNGRaQmNHREFJWWVIWkVfdnFoSXJTVDU4WEZuWVFGenV5SzRxNXRrYTQ3cFVTTEtBdlphaC1KdzdORG5UUndqOU5Cb21IY2s0Wm11eTIwaGg3YlpUN09RQlpSMFhVRnZfR2dEWGZoNDVLc18tblg3R3pBRjAycWRtYURiTnlZOERPWWRVTUx5ZmlYSFNtOHU1NkVSODJIdzVRZGRBMi1aZmFscg?oc=5) | PBS | 2026-09-11 16:37 |
| 6 | [美國第十三修正案與廢除奴役倡議](https://news.google.com/rss/articles/CBMiYkFVX3lxTE1PSUFrNTR1QWtsQUp5MVNpR0FScFNTdlUyS2hYQjNZV0pQV3kzWTJpNjZrRmZIUXhYbG41UWtsN21abm1COTFfbGZXMlkwTjljeUVMdGs0MzloRXpWblJrWExn?oc=5) | Freedom United | 2026-09-09 19:01 |

這 6 筆都來自實際外部回應，並非固定測試新聞。但第 2、6 筆從標題看屬政策／倡議資料，其他也包含背景追蹤與分析。因此本次應視為「6 筆真實 RSS 來源資料」的流程驗收，不能宣稱已完成 6 篇全文新聞的 AI 風險判讀。

## 追加驗證結果

| 驗證 | 結果 |
|---|---|
| 首次保存 | 新增 6 筆，狀態 pending，延遲未知 |
| 模擬模型服務失敗 | 6 筆標記 failed，不補入 7 天，不成為有效風險輸入 |
| 12 筆重複輸入 | 資料庫仍為 6 筆，逐篇分析僅執行 6 筆 |
| 嚴格輸出驗證 | 0、未知、5 天、無關資料分開處理；字串天數與負數遭拒絕 |
| 登錄事件 | 未知或失敗的新聞不能登錄為已知風險事件 |
| 重試 | 只重試 2 筆失敗資料；已成功資料不重做逐篇分析 |
| 再次刷新 | 新增 0 筆、逐篇分析 0 筆 |
| 原始資料保存 | RSS 標題、摘要、URL、來源、時間、搜尋國別在所有步驟後均保持原值 |
| 一次性排程測試 | 首次失敗後重試成功；相同工作識別碼再次執行為 skipped |
| 同時執行防護 | 持有新聞鎖時，另一刷新回傳 busy |
| 地區一致性 | 用合成供應商／採購／庫存驗證台灣北區只匹配北區 |
| 零值保存 | 0% 與 0 天在重新連接 SQLite 後仍保留 |
| 隔離限制 | 背景排程停用；擷取完成後阻擋所有後續對外連線 |

上述天數、地區與百分比是刻意注入的驗收案例，不是對這些新聞的真實風險結論。原始 RSS 搜尋國別「美國」亦不能直接等同事件受影響地區。

## 本次發現的限制

- 6 筆 RSS 摘要基本上是標題與來源名稱的重複，不能替代新聞全文。
- 目前 RSS 搜尋仍可能包含政策、背景或倡議頁面；本次沒有擴大實作來源品質篩選。
- 原始文字中的 HTML entity（例如 `&nbsp;`）保留在擷取資料內。
- 尚未驗證 GNews API、Gemini 模型判斷品質、文章全文擷取或真實事件延遲推估。

本次沒有發現第一階段資料處理斷言失敗；也沒有改動既有業務程式。新增的只有一次性驗收腳本與此報告。第一階段原有 387 項測試結果維持在既有報告，本次沒有把 14 項腳本斷言混算為 pytest 測試數。

## 驗收資料與重播

- 資料庫：`C:\新EPR系統\ERP-batch1-isolated\.isolated\live-news-20260913T111447765066Z\acceptance.db`
- 原始擷取：`C:\新EPR系統\ERP-batch1-isolated\.isolated\live-news-20260913T111447765066Z\news-capture.json`
- 各階段計數與狀態：`C:\新EPR系統\ERP-batch1-isolated\.isolated\live-news-20260913T111447765066Z\acceptance-results.json`
- 離線重播結果：`.isolated/live-news-20260913T111702955359Z/acceptance-results.json`。

```powershell
Set-Location 'C:\新EPR系統\ERP-batch1-isolated'
$py = 'C:\新EPR系統\AI-Risk-Based-Inventory-ERP-new\.venv\Scripts\python.exe'
& $py scripts/accept_live_news.py --snapshot '.isolated/live-news-20260913T111447765066Z/news-capture.json'
```

此命令不再抓取外部新聞，會建立新的獨立驗收資料庫。原工作區與原有檢查資料庫均未修改，`.env` 未修改，未合併、推送或部署。
