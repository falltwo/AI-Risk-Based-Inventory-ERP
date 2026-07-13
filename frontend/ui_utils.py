"""
frontend/ui_utils.py
前端共用小工具（F14）：錯誤脫敏顯示。

原則：畫面只給使用者「哪個功能失敗＋錯誤類型」，
完整 exception（可能含內部路徑/SQL/供應商回應）只進伺服器端 log。
"""

import logging
import traceback

import streamlit as st

_logger = logging.getLogger("erp.ui")


def show_error(user_msg: str, exc: Exception | None = None) -> None:
    """
    對使用者顯示脫敏後的錯誤，完整細節寫入 log。
      show_error("AI 快析失敗", e)
    畫面 → 「AI 快析失敗（TimeoutError），詳情請洽系統紀錄。」
    log   → 完整 traceback。
    """
    if exc is not None:
        _logger.error("%s: %s\n%s", user_msg, exc, traceback.format_exc())
        st.error(f"{user_msg}（{type(exc).__name__}），詳情請洽系統紀錄。")
    else:
        _logger.error(user_msg)
        st.error(user_msg)
