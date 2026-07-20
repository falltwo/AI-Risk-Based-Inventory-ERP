"""
backend/scheduler.py
背景任務排程器：定時自動抓取最新供應鏈新聞。
"""

import os
import threading
import time
from backend.access_control import RISK_WORKSPACE_WRITE, require_capability
from backend.supply_chain_news import refresh_news_for_countries
from backend.supply_chain_risk import get_suppliers_for_map

# 記錄排程器是否已啟動，避免重複執行
_scheduler_started = False


def refresh_supply_chain_news_once(*, actor: str) -> dict:
    """Run one authorized refresh; the actor is checked live by the service."""
    actor = str(actor or "").strip()
    if not actor:
        raise PermissionError("背景新聞刷新需要 ERP_SCHEDULER_ACTOR")
    require_capability(actor, RISK_WORKSPACE_WRITE)

    suppliers = get_suppliers_for_map()
    countries = []
    if suppliers is not None and not suppliers.empty and "country" in suppliers.columns:
        countries = suppliers["country"].dropna().unique().tolist()
        countries = [str(country).strip() for country in countries if str(country).strip()]
    if not countries:
        countries = ["台灣", "日本", "美國", "南韓", "中國", "越南", "墨西哥"]

    return refresh_news_for_countries(
        countries,
        max_per_country=5,
        actor=actor,
    )

def start_background_jobs():
    global _scheduler_started
    if _scheduler_started:
        return True
    actor = os.getenv("ERP_SCHEDULER_ACTOR", "").strip()
    if not actor:
        print("Background scheduler disabled: ERP_SCHEDULER_ACTOR is not configured.")
        return False
    _scheduler_started = True

    def run_jobs():
        while True:
            try:
                # 每天定時抓取一次新聞 (每 24 小時)
                time.sleep(10) # 系統啟動後延遲 10 秒再抓
                refresh_supply_chain_news_once(actor=actor)
            except Exception as e:
                print(f"Background scheduler error: {e}")
            
            # 休息 24 小時 (可以視需求調整頻率)
            time.sleep(24 * 60 * 60)

    # 設定為 Daemon Thread，讓主程式結束時能隨之關閉
    job_thread = threading.Thread(target=run_jobs, daemon=True)
    job_thread.start()
    return True
