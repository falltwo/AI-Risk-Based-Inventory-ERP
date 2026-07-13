"""
tests/test_error_masking.py
F14：前端錯誤脫敏 —— 畫面只給錯誤類型，exception 細節不落 UI。
"""

import streamlit as st

from frontend.ui_utils import show_error


def test_show_error_masks_exception_details(monkeypatch):
    captured = {}
    monkeypatch.setattr(st, "error", lambda msg: captured.update(msg=msg))

    secret = r"C:\internal\secret_path\db.sqlite 連線失敗 token=abc123"
    show_error("資料讀取失敗", RuntimeError(secret))

    msg = captured["msg"]
    assert "資料讀取失敗" in msg
    assert "RuntimeError" in msg          # 給錯誤類型，方便回報
    assert "secret_path" not in msg       # 細節不上畫面
    assert "abc123" not in msg


def test_show_error_without_exception(monkeypatch):
    captured = {}
    monkeypatch.setattr(st, "error", lambda msg: captured.update(msg=msg))

    show_error("純訊息錯誤")
    assert captured["msg"] == "純訊息錯誤"
