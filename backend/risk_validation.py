"""Fail-closed validators: zero is a measurement, None is unknown."""
import json
import math
import numbers
import re

EVENT_TYPES = {"戰爭", "氣候", "罷工", "政策", "交通", "其他", "地震", "天候", "政治", "疫情"}


def number(value, *, maximum, integer=False, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError("Expected a JSON number")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError("Number out of range")
    if integer and not value.is_integer():
        raise ValueError("Expected an integer")
    return int(value) if integer else value


def text(value):
    if not isinstance(value, str):
        raise ValueError("Expected text")
    return value.strip()


def json_payload(raw):
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    def unique_object(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("Duplicate JSON key")
            result[k] = v
        return result
    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError("Non-finite JSON")))


def failed_analysis(code="invalid_output"):
    return dict(analysis_status="failed", analysis_error=code, is_relevant=None,
                country="", region="", event_type=None, estimated_delay=None, chinese_summary=None)


def parse_news_batch(raw, count):
    payload = json_payload(raw)
    rows = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Expected results array")
    results = [failed_analysis("missing_result") for _ in range(count)]
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Expected result object")
        idx = row.get("news_id")
        if type(idx) is not int or not 0 <= idx < count or idx in seen:
            raise ValueError("Invalid or duplicate news_id")
        seen.add(idx)
        try:
            if row["相關性"] not in ("YES", "NO"):
                raise ValueError("Invalid relevance")
            etype = text(row["事件類型"])
            if etype not in EVENT_TYPES:
                raise ValueError("Invalid event type")
            country, region = text(row["國家"]), text(row["地區"])
            delay = number(row["預計延遲"], maximum=365, integer=True, nullable=True)
            if row["相關性"] == "NO" and delay not in (None, 0):
                raise ValueError("Irrelevant news cannot have delay")
            results[idx] = dict(analysis_status="succeeded", analysis_error=None,
                is_relevant=row["相關性"] == "YES", country="" if country == "不明" else country,
                region="" if region == "不明" else region, event_type=etype,
                estimated_delay=delay, chinese_summary=text(row["繁體中文簡要"]))
        except (KeyError, ValueError, TypeError):
            results[idx] = failed_analysis()
    return results
