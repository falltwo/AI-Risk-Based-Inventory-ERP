"""
backend/scheduler.py
背景任務排程器：定時自動抓取最新供應鏈新聞。
"""

import threading
import time
from backend.supply_chain_news import refresh_news_for_countries
from backend.supply_chain_risk import get_suppliers_for_map

# 記錄排程器是否已啟動，避免重複執行
_scheduler_started = False

def start_background_jobs():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def run_jobs():
        while True:
            try:
                # 每天定時抓取一次新聞 (每 24 小時)
                time.sleep(10) # 系統啟動後延遲 10 秒再抓
                _suppliers = get_suppliers_for_map()
                countries = []
                if _suppliers is not None and not _suppliers.empty and "country" in _suppliers.columns:
                    countries = _suppliers["country"].dropna().unique().tolist()
                    countries = [str(c).strip() for c in countries if str(c).strip()]
                if not countries:
                    countries = ["台灣", "日本", "美國", "南韓", "中國", "越南", "墨西哥"]
                
                # 自動更新新聞 (不使用 API key，使用 RSS 備援)
                refresh_news_for_countries(countries, api_key=None, max_per_country=5)
            except Exception as e:
                print(f"Background scheduler error: {e}")
            
            # 休息 24 小時 (可以視需求調整頻率)
            time.sleep(24 * 60 * 60)

    # 設定為 Daemon Thread，讓主程式結束時能隨之關閉
    job_thread = threading.Thread(target=run_jobs, daemon=True)
    job_thread.start()
