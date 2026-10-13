"""One exact geographic contract for Python and SQLite consumers.

Comma-separated selectors are OR; country + subregion is AND. Spaces inside
country names are preserved. Empty selectors never match. No SQL wildcards.
"""
import re

REGION_COUNTRY_MAP = {
    "亞洲": ["台灣", "日本", "中國", "南韓", "北韓", "越南", "泰國", "新加坡", "馬來西亞", "印尼", "菲律賓", "印度", "香港", "澳門", "緬甸", "柬埔寨", "寮國"],
    "東亞": ["台灣", "日本", "中國", "南韓", "北韓", "香港", "澳門"],
    "東南亞": ["越南", "泰國", "新加坡", "馬來西亞", "印尼", "菲律賓", "緬甸", "柬埔寨", "寮國"],
    "歐洲": ["德國", "法國", "英國", "義大利", "西班牙", "荷蘭", "波蘭", "比利時", "奧地利", "瑞士"],
    "北美": ["美國", "加拿大", "墨西哥"],
    "中東": ["伊朗", "沙烏地阿拉伯", "阿拉伯聯合大公國", "以色列", "卡達", "伊拉克", "科威特", "約旦", "黎巴嫩", "敘利亞", "土耳其"],
    "非洲": ["埃及", "南非", "摩洛哥", "奈及利亞"],
}
ALIASES = {
    "臺灣": "台灣", "taiwan": "台灣", "tw": "台灣",
    "japan": "日本", "jp": "日本", "united states": "美國", "usa": "美國", "us": "美國",
    "韓國": "南韓", "south korea": "南韓", "korea": "南韓", "kr": "南韓",
    "阿聯酋": "阿拉伯聯合大公國", "阿聯": "阿拉伯聯合大公國", "uae": "阿拉伯聯合大公國",
    "china": "中國", "vietnam": "越南", "germany": "德國", "united kingdom": "英國",
    "singapore": "新加坡", "canada": "加拿大", "mexico": "墨西哥",
}


def normalize(value):
    value = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    return ALIASES.get(value, value)


def _parts(value):
    return [v.strip() for v in re.split(r"[,，、;；]", str(value or "")) if v.strip()]


def split_location(value):
    value = str(value or "").strip()
    if "|" in value:
        c, r = value.split("|", 1)
        return normalize(c), normalize(r)
    known = set(ALIASES) | set(ALIASES.values()) | set(REGION_COUNTRY_MAP)
    known.update(c for countries in REGION_COUNTRY_MAP.values() for c in countries)
    for c in sorted(known, key=len, reverse=True):
        if value.casefold().startswith(c.casefold() + " "):
            return normalize(c), normalize(value[len(c):])
    return "", normalize(value)


def _country_match(selector, country):
    return selector == country or country in REGION_COUNTRY_MAP.get(selector, [])


def matches_location(country, region, selector_region=None, selector_country=None):
    country, region = normalize(country), normalize(region)
    countries = [normalize(c) for c in _parts(selector_country)]
    regions = _parts(selector_region)
    if not countries and not regions:
        return False
    if countries and not any(_country_match(c, country) for c in countries):
        return False
    if not regions:
        return True
    for value in regions:
        exact = normalize(value)
        if exact in {country, region, f"{country} {region}", f"{country}|{region}"}:
            return True
        c, r = split_location(value)
        if c:
            if _country_match(c, country) and (r == c or r == region):
                return True
        elif _country_match(r, country) or r == region:
            return True
    return False


def expanded_region_where(region, country, prefix=""):
    if prefix not in ("", "s.", "p.", "c."):
        raise ValueError("Unsupported SQL alias")
    return [f"erp_region_matches({prefix}country, {prefix}region, ?, ?) = 1"], [region, country]


def connect_db(path):
    import sqlite3
    conn = sqlite3.connect(path)
    conn.create_function("erp_region_matches", 4, matches_location, deterministic=True)
    return conn
