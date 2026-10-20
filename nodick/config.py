"""NoDick configuration — loaded from .env"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _int_env(name: str, default: int = 0) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        return default


def _float_env(name: str, default: float = 0.0) -> float:
    value = os.getenv(name, str(default)).strip()
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT

    # Bot
    bot_token: str = os.getenv("BOT_TOKEN", "").strip()
    # Fallback to the owner's ID if ADMIN_ID is missing or blank in the env.
    # Prevents a stray/blank dashboard ADMIN_ID from silently stripping admin
    # (dashboard env overrides render.yaml — a recurring NoDick deploy trap).
    admin_id: int = _int_env("ADMIN_ID") or 6001922744

    # Telethon
    telegram_api_id: int = _int_env("TELEGRAM_API_ID")
    telegram_api_hash: str = os.getenv("TELEGRAM_API_HASH", "").strip()
    telegram_phone: str = os.getenv("TELEGRAM_PHONE", "").strip()
    user_session: str = os.getenv(
        "TELEGRAM_USER_SESSION", str(ROOT / "runtime" / "nodick_user")
    )
    default_import_channel: str = os.getenv("IMPORT_CHANNEL_ID", "").strip()

    # Database
    db_path: str = os.getenv("DB_PATH", str(ROOT / "runtime" / "nodick.db"))
    database_url: str = os.getenv("DATABASE_URL", "").strip()

    bot_username: str = os.getenv("BOT_USERNAME", "Moyechan_bot").strip().lstrip("@")

    # Metadata APIs
    stashdb_api_key: str = os.getenv("STASHDB_API_KEY", "").strip()
    stashdb_graphql_url: str = os.getenv(
        "STASHDB_GRAPHQL_URL", "https://stashdb.org/graphql"
    )
    fansdb_api_key: str = os.getenv("FANSDB_API_KEY", "").strip()
    fansdb_graphql_url: str = os.getenv(
        "FANSDB_GRAPHQL_URL", "https://fansdb.cc/graphql"
    )

    # Optional sponsored Premium passes (AdLinkFly-compatible GET API).
    shortener_domain: str = os.getenv("SHORTENER_DOMAIN", "").strip()
    shortener_api_key: str = field(default=os.getenv("SHORTENER_API_KEY", "").strip(), repr=False)
    shortener_api_path: str = os.getenv("SHORTENER_API_PATH", "/api").strip()
    shortener_api_param: str = os.getenv("SHORTENER_API_PARAM", "api").strip()
    shortener_url_param: str = os.getenv("SHORTENER_URL_PARAM", "url").strip()
    shortener_url_field: str = os.getenv("SHORTENER_URL_FIELD", "shortenedUrl").strip()
    shortener_link_domains: str = os.getenv("SHORTENER_LINK_DOMAINS", "").strip()
    shortener_tutorial_video: str = os.getenv("SHORTENER_TUTORIAL_VIDEO", "").strip()
    shortener_enabled: bool = os.getenv("SHORTENER_ENABLED", "true").lower() == "true"

    # Matching
    match_threshold: float = _float_env("MATCH_THRESHOLD", 0.0)
    auto_rename_threshold: float = _float_env("AUTO_RENAME_THRESHOLD", 0.90)
    debug_matching: bool = os.getenv("DEBUG_MATCHING", "false").lower() == "true"

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    logs_channel_id: int = _int_env("LOGS_CHANNEL_ID")

    @property
    def configured_for_bot(self) -> bool:
        return bool(self.bot_token and self.admin_id)

    @property
    def configured_for_user_import(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash)

    @property
    def stash_configured(self) -> bool:
        return bool(self.stashdb_api_key)

    @property
    def fansdb_configured(self) -> bool:
        return bool(self.fansdb_api_key)


settings = Settings()
