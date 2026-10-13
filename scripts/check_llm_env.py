"""
scripts/check_llm_env.py
檢查 .env 的 LLM 端點設定（LLM_MODEL / OPENAI_API_BASE / OPENAI_API_KEY …），
並可選擇真的打一次模型（走 backend.llm_client.complete_text，跟分析頁同一條路）。

用法：
  python scripts/check_llm_env.py            # 只檢查設定 + 查端點模型清單
  python scripts/check_llm_env.py --call     # 額外送一句 "ping" 確認金鑰可用
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = ROOT_DIR / ".env"


def mask_value(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check LLM endpoint settings in .env.")
    parser.add_argument("--env", default=str(DEFAULT_ENV_PATH), help="Path to the .env file.")
    parser.add_argument("--call", action="store_true", help="Send one real completion to verify the key.")
    return parser.parse_args()


def _provider_and_name(model: str) -> tuple[str, str]:
    provider, _, name = model.partition("/")
    return (provider, name) if name else ("", model)


def _list_models(api_base: str) -> list[str] | None:
    """GET {api_base}/models（OpenAI 相容端點多半不需金鑰）。失敗回 None。"""
    try:
        req = urllib.request.Request(f"{api_base.rstrip('/')}/models",
                                     headers={"User-Agent": "erp-inventory/check_llm_env"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.load(resp)
        return [m["id"] for m in payload.get("data", [])]
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError) as exc:
        print(f"- model list: unavailable ({type(exc).__name__})")
        return None


def main() -> int:
    args = parse_args()
    env_path = Path(args.env)
    if env_path.exists():
        load_dotenv(env_path)
        print(f"Loaded env file: {env_path}")
    else:
        print(f"Env file not found: {env_path}")

    model = os.environ.get("LLM_MODEL", "").strip()
    analysis_model = os.environ.get("LLM_ANALYSIS_MODEL", "").strip()
    fallbacks = [m.strip() for m in os.environ.get("LLM_FALLBACK_MODELS", "").split(",") if m.strip()]
    api_base = os.environ.get("OPENAI_API_BASE", "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()

    problems: list[str] = []
    print("\nModel settings:")
    print(f"- LLM_MODEL: {model or 'MISSING'}")
    print(f"- LLM_ANALYSIS_MODEL: {analysis_model or '(same as LLM_MODEL)'}")
    print(f"- LLM_FALLBACK_MODELS: {', '.join(fallbacks) if fallbacks else '(none; primary failure will surface as an error)'}")
    print(f"- LLM_EXTRA_HEADERS: {os.environ.get('LLM_EXTRA_HEADERS', '').strip() or '(none)'}")
    print(f"- LLM_TIMEOUT: {os.environ.get('LLM_TIMEOUT', '').strip() or '(default 120s)'}")
    if not model:
        problems.append("LLM_MODEL is empty")

    provider, model_name = _provider_and_name(model)
    print("\nProvider keys:")
    if provider == "openai":
        print(f"- OPENAI_API_BASE: {api_base or '(default api.openai.com)'}")
        print(f"- OPENAI_API_KEY: {'OK (' + mask_value(api_key) + ')' if api_key else 'MISSING'}")
        if not api_key:
            problems.append("OPENAI_API_KEY is empty")
        if api_base:
            ids = _list_models(api_base)
            if ids is not None:
                print(f"- model list: {len(ids)} models at {api_base}")
                if model_name in ids:
                    print(f"- {model_name}: found on endpoint")
                else:
                    problems.append(f"{model_name!r} is not in the endpoint model list")
                    print(f"- {model_name}: NOT FOUND on endpoint")
    elif provider == "gemini":
        print(f"- GEMINI_API_KEY: {'OK (' + mask_value(gemini_key) + ')' if gemini_key else 'MISSING'}")
        if not gemini_key:
            problems.append("GEMINI_API_KEY is empty")
    elif model:
        print(f"- provider {provider!r}: key is read by litellm from its own env var; not checked here")

    for fb in fallbacks:
        fb_provider, _ = _provider_and_name(fb)
        if fb_provider == "gemini" and not gemini_key:
            problems.append(f"fallback {fb} needs GEMINI_API_KEY")
        if fb_provider == "openai" and not api_key:
            problems.append(f"fallback {fb} needs OPENAI_API_KEY")

    if args.call and not problems:
        print("\nLive call:")
        sys.path.insert(0, str(ROOT_DIR))
        from backend.llm_client import complete_text
        try:
            reply = complete_text("Reply with the single word: pong", temperature=0, tag="check_llm_env")
            print(f"- {model}: OK -> {reply[:80]!r}")
        except Exception as exc:  # 診斷工具，錯誤原樣印出
            problems.append(f"live call failed: {type(exc).__name__}: {str(exc)[:300]}")
            print(f"- {model}: FAILED")

    if problems:
        print("\nLLM readiness: NOT READY")
        for p in problems:
            print(f"- {p}")
        return 1
    print("\nLLM readiness: READY" + ("" if args.call else " (settings only; add --call to verify the key)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
