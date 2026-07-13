from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = ROOT_DIR / ".env"
REQUIRED_ENV_VARS = [
    "LINE_CHANNEL_ACCESS_TOKEN",
    "LINE_CHANNEL_SECRET",
    "GEMINI_API_KEY",
]
REQUIRED_IMPORTS = [
    ("fastapi", "fastapi"),
    ("linebot", "line-bot-sdk"),
    ("dotenv", "python-dotenv"),
    ("google.genai", "google-genai"),
]


def mask_value(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check LINE Bot .env values and required Python packages.")
    parser.add_argument(
        "--env",
        default=str(DEFAULT_ENV_PATH),
        help="Path to the .env file. Defaults to project root .env.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_path = Path(args.env)

    if env_path.exists():
        load_dotenv(env_path)
        print(f"Loaded env file: {env_path}")
    else:
        print(f"Env file not found: {env_path}")
        print("Create it from .env.example, then fill in real LINE and Gemini keys.")

    missing_env = []
    print("\nEnvironment variables:")
    for key in REQUIRED_ENV_VARS:
        value = os.environ.get(key, "").strip()
        if value:
            print(f"- {key}: OK ({mask_value(value)})")
        else:
            print(f"- {key}: MISSING")
            missing_env.append(key)

    briefing_enabled = os.environ.get("LINE_BRIEFING_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    briefing_user_ids = [
        value.strip()
        for value in os.environ.get("LINE_BRIEFING_USER_IDS", "").split(",")
        if value.strip()
    ]
    briefing_config_error = briefing_enabled and not briefing_user_ids
    print("\nMorning briefing:")
    print(f"- enabled: {'YES' if briefing_enabled else 'NO (safe default)'}")
    print(f"- allowlisted recipients: {len(set(briefing_user_ids))}")
    if briefing_config_error:
        print("- configuration: INVALID (enabled but LINE_BRIEFING_USER_IDS is empty)")

    missing_imports = []
    print("\nPython packages:")
    for module_name, package_name in REQUIRED_IMPORTS:
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            print(f"- {package_name}: MISSING")
            missing_imports.append(package_name)
        else:
            print(f"- {package_name}: OK")

    if missing_env or missing_imports or briefing_config_error:
        print("\nLINE Bot readiness: NOT READY")
        if missing_env:
            print("Missing env vars: " + ", ".join(missing_env))
        if missing_imports:
            print("Install missing packages with: pip install -r requirements.txt")
        if briefing_config_error:
            print("Set LINE_BRIEFING_USER_IDS or disable LINE_BRIEFING_ENABLED.")
        return 1

    print("\nLINE Bot readiness: READY")
    print("Next startup command: python \"line bot/bot_server.py\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
