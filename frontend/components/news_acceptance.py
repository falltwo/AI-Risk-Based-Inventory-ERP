"""Show captured real news and acceptance evidence inside the risk workspace."""
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from backend.isolated_runtime import news_capture
from backend.supply_chain_news import get_news_from_db


def render_news_acceptance():
    if os.getenv("ERP_ISOLATED_TEST") != "1":
        return
    capture = news_capture()
    if not capture:
        return
    source = "GNews API" if capture["source"] == "gnews" else "Google News RSS"
    report_path = os.getenv("ERP_NEWS_ACCEPTANCE", "")
    report = json.loads(Path(report_path).read_text(encoding="utf-8")) if report_path else {}
    st.subheader("📰 真實新聞追加驗收")
    st.info(f"新聞來源：{source}｜這是已抓取的真實新聞快照。AI 分析為模擬驗收，非實際風險判斷。")
    st.caption(f"抓取時間：{capture['captured_at']}｜此頁讀取獨立檢查資料庫；重播不再連線抓新聞。")
    latest_attempt = Path(__file__).resolve().parents[2] / ".isolated" / "gnews-latest-attempt.json"
    if latest_attempt.is_file():
        attempt = json.loads(latest_attempt.read_text(encoding="utf-8"))
        if attempt.get("status") == "capture_failed":
            st.warning(f"最近一次 GNews 擷取失敗（HTTP {attempt.get('http_status', '未知')}）：{attempt.get('message', '請核對 API 設定')}。目前下表來源是 {source}，不是該次 GNews 的成功結果。")

    rows = get_news_from_db(limit=1000)
    captured_urls = {a["url"] for a in capture["articles"]}
    rows = [r for r in rows if r["url"] in captured_urls]
    c1, c2, c3 = st.columns(3)
    c1.metric("本批真實來源資料", len(capture["articles"]))
    c2.metric("目前資料庫筆數", len(rows))
    c3.metric("匯入時流程驗收", f"{len(report.get('checks', []))} 項通過")
    states = {"succeeded": "成功", "failed": "失敗", "pending": "待分析", "legacy_unverified": "舊資料待確認"}
    table = []
    for row in rows:
        table.append({"新聞標題": row["title"], "出版來源": row.get("source") or source,
                      "發布時間": row.get("published_at"),
                      "分析狀態（模擬）": states.get(row.get("analysis_status"), "未知"),
                      "延遲（模擬）": "未知" if row.get("estimated_delay") is None else f"{row['estimated_delay']} 天",
                      "原文連結": row["url"]})
    if table:
        st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch",
                     column_config={"原文連結": st.column_config.LinkColumn("原文", display_text="開啟來源")})
    with st.expander("逐則查看來源摘要"):
        for row in rows:
            st.markdown(f"**{row['title']}**")
            st.write(row.get("summary") or "來源沒有提供摘要。")
            st.caption(f"來源：{row.get('source') or source}｜發布：{row.get('published_at') or '未知'}")
    with st.expander("查看追加驗收過程"):
        phases = report.get("phases", {})
        labels = {"pending": "初次保存", "outage": "模擬模型中斷", "strict_validation": "輸出驗證與去重",
                  "retry": "只重試失敗項目", "deduped_replay": "再次重播"}
        records = []
        for key, label in labels.items():
            phase = phases.get(key, {})
            records.append({"步驟": label, "新增": phase.get("saved_count", 0),
                            "重複": phase.get("duplicate_count", 0), "分析成功": phase.get("analyzed_count", 0),
                            "分析失敗": phase.get("failed_count", 0)})
        st.dataframe(pd.DataFrame(records), hide_index=True, width="stretch")
        st.caption("此處是抓取後的驗收紀錄；天數與地區是測試案例，不是新聞的實際影響。")
