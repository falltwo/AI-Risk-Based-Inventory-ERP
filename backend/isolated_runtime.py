"""Deterministic local fixtures and a network guard for the isolated launcher."""
import ipaddress
import os
import json
import re
import socket
from pathlib import Path


def news_capture():
    """Optional real-source snapshot selected by the local review launcher."""
    path = os.getenv("ERP_NEWS_CAPTURE", "")
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def block_external_network():
    if getattr(socket, "_erp_isolated", False):
        return
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def allowed(host):
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def check(address):
        if isinstance(address, tuple) and not allowed(address[0]):
            raise OSError("Isolated ERP: external network and notifications are disabled")

    def connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not allowed(host):
            raise OSError("Isolated ERP: external DNS is disabled")
        return original_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
    socket._erp_isolated = True


def fixture_news(country):
    capture = news_capture()
    if capture:
        from .region_matching import normalize
        return [dict(a) for a in capture["articles"] if normalize(a.get("country")) == normalize(country)]
    rows = [
        ("zero", "港口恢復營運 [ZERO]", "確認目前無延遲。"),
        ("delay", "港口罷工 [DELAY]", "固定測試事件：延遲五天。"),
        ("unknown", "交期尚未確認 [UNKNOWN]", "目前沒有可靠的延遲天數。"),
        ("invalid", "模型輸出格式錯誤案例 [INVALID]", "保留此原文供人工檢查。"),
    ]
    if os.getenv("ERP_ISOLATED_SCENARIO", "mixed") == "success":
        rows = [row for row in rows if row[0] != "invalid"]
    return [dict(country=country, region=None, title=f"{country} {title}", summary=summary,
                 url=f"https://fixture.invalid/{country}/{key}", source="固定測試資料",
                 published_at="2026-09-13 08:00", relevance_tag="supply_chain")
            for key, title, summary in rows]


def fixture_completion(prompt, tag):
    if tag == "analysis:news_batch":
        results = []
        for idx, body in re.findall(r"【新聞編號 (\d+)】\n(.*?)(?=【新聞編號|$)", str(prompt), re.S):
            country = next((c for c in ("台灣", "日本", "美國", "南韓", "中國", "越南", "墨西哥", "德國", "新加坡") if body.startswith(c)), "台灣")
            delay = 0 if "[ZERO]" in body else None if "[UNKNOWN]" in body else "invalid" if "[INVALID]" in body else 5
            results.append({"news_id": int(idx), "相關性": "YES", "國家": country, "地區": "不明",
                            "事件類型": "交通", "預計延遲": delay, "繁體中文簡要": "固定模擬分析；非即時新聞。"})
        return json.dumps({"results": results}, ensure_ascii=False)
    if tag == "analysis:heatmap":
        return json.dumps({"摘要": "固定測試摘要：台灣北區確認為 0%／0 天，日本為 65%／5 天。",
            "更新": [{"地區": "台灣 北區", "風險": 0}, {"地區": "日本", "風險": 65}],
            "事件": [{"類型": "交通", "地區": "北區", "國家": "台灣", "延遲天數": 0, "描述": "固定零值測試"},
                     {"類型": "罷工", "地區": "日本", "國家": "日本", "延遲天數": 5, "描述": "固定延遲測試"}]}, ensure_ascii=False)
    if tag == "analysis:po_alternative":
        return '{"results": []}'
    return "隔離測試環境：這是固定模擬回應，未呼叫外部模型或發送通知。"
