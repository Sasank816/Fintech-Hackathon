"""Settings from the environment. `.env` in the repo root wins over machine-level variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    anthropic_api_key: str = ""
    model: str = "claude-opus-5-5"
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    gmail_address: str = ""
    gmail_app_password: str = ""
    mail_mode: str = "outbox"  # gmail | outbox
    memory_backend: str = "local"  # cognee | local
    business_name: str = "Hollis Electrical Ltd"
    demo_emails: dict[str, str] = field(default_factory=dict)
    data_dir: Path = ROOT / "data"
    web_dir: Path = ROOT / "web"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clause.db"

    @property
    def ratecard_path(self) -> Path:
        return self.data_dir / "ratecard.json"


def load_settings(env_file: str | Path | None = None) -> Settings:
    from dotenv import load_dotenv

    load_dotenv(env_file or ROOT / ".env", override=True)
    env = os.environ.get
    return Settings(
        anthropic_api_key=env("ANTHROPIC_API_KEY", "").strip(),
        model=env("CLAUSE_MODEL", "claude-opus-5-5").strip(),
        stripe_secret_key=env("STRIPE_SECRET_KEY", "").strip(),
        stripe_webhook_secret=env("STRIPE_WEBHOOK_SECRET", "").strip(),
        gmail_address=env("GMAIL_ADDRESS", "").strip(),
        gmail_app_password=env("GMAIL_APP_PASSWORD", "").replace(" ", "").strip(),
        mail_mode=env("MAIL_MODE", "outbox").strip().lower(),
        memory_backend=env("MEMORY_BACKEND", "local").strip().lower(),
        business_name=env("BUSINESS_NAME", "Hollis Electrical Ltd").strip(),
        demo_emails={k: env(f"DEMO_EMAIL_{k.upper()}", "").strip() for k in ("acme", "bright", "northwind")},
    )
