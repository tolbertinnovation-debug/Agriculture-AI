"""Runtime configuration, read from environment variables.

Every setting has a safe development default so the platform runs locally with
no configuration at all (SQLite, console SMS gateway, rule-based AI fallback).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("AGRI_DATABASE_URL", "sqlite:///./agri_ai.db"))
    # Token for platform operators (creating partner organisations, running jobs).
    admin_token: str = field(default_factory=lambda: os.environ.get("AGRI_ADMIN_TOKEN", "dev-admin-token"))
    # Secret used to sign farmer session tokens.
    secret_key: str = field(default_factory=lambda: os.environ.get("AGRI_SECRET_KEY", "dev-secret-change-me"))
    # Shared secret SMS/USSD/WhatsApp gateways must send (header X-Webhook-Secret or ?secret=).
    webhook_secret: str = field(default_factory=lambda: os.environ.get("AGRI_WEBHOOK_SECRET", "dev-webhook-secret"))

    # AI. Without an API key the platform uses its rule-based fallbacks.
    anthropic_api_key: str | None = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY") or None)
    llm_model: str = field(default_factory=lambda: os.environ.get("AGRI_LLM_MODEL", "claude-opus-5-5"))
    llm_enabled: bool = field(default_factory=lambda: _bool("AGRI_LLM_ENABLED", True))
    # Per-farmer daily cap on model calls (advice + photo diagnosis) to keep partner costs predictable.
    ai_daily_limit: int = field(default_factory=lambda: int(os.environ.get("AGRI_AI_DAILY_LIMIT", "20")))
    # Hosts inbound WhatsApp/MMS media may be downloaded from (prevents SSRF via webhook payloads).
    media_host_allowlist: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            h.strip()
            for h in os.environ.get("AGRI_MEDIA_HOST_ALLOWLIST", "api.twilio.com,media.twiliocdn.com,lookaside.fbsbx.com").split(",")
            if h.strip()
        )
    )

    # Outbound messaging: "console" (log only), "africastalking" or "twilio".
    sms_gateway: str = field(default_factory=lambda: os.environ.get("AGRI_SMS_GATEWAY", "console"))
    africastalking_username: str = field(default_factory=lambda: os.environ.get("AT_USERNAME", "sandbox"))
    africastalking_api_key: str | None = field(default_factory=lambda: os.environ.get("AT_API_KEY"))
    africastalking_sender_id: str | None = field(default_factory=lambda: os.environ.get("AT_SENDER_ID"))
    twilio_account_sid: str | None = field(default_factory=lambda: os.environ.get("TWILIO_ACCOUNT_SID"))
    twilio_auth_token: str | None = field(default_factory=lambda: os.environ.get("TWILIO_AUTH_TOKEN"))
    twilio_from_number: str | None = field(default_factory=lambda: os.environ.get("TWILIO_FROM_NUMBER"))

    # Weather: "open-meteo" (free, no key) or "static" (deterministic, offline).
    weather_provider: str = field(default_factory=lambda: os.environ.get("AGRI_WEATHER_PROVIDER", "open-meteo"))
    weather_cache_minutes: int = field(default_factory=lambda: int(os.environ.get("AGRI_WEATHER_CACHE_MINUTES", "180")))

    upload_dir: str = field(default_factory=lambda: os.environ.get("AGRI_UPLOAD_DIR", "./uploads"))
    max_upload_bytes: int = field(default_factory=lambda: int(os.environ.get("AGRI_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024))))

    # Expose the one-time password in the API response (development only!).
    dev_expose_otp: bool = field(default_factory=lambda: _bool("AGRI_DEV_EXPOSE_OTP", True))
    farmer_token_days: int = field(default_factory=lambda: int(os.environ.get("AGRI_FARMER_TOKEN_DAYS", "30")))

    # Diagnoses below this confidence are routed to an extension officer for review.
    review_confidence_threshold: float = field(
        default_factory=lambda: float(os.environ.get("AGRI_REVIEW_CONFIDENCE_THRESHOLD", "0.6"))
    )
    # Outbreak detection: N confirmed reports of the same problem within R km over D days.
    outbreak_min_reports: int = field(default_factory=lambda: int(os.environ.get("AGRI_OUTBREAK_MIN_REPORTS", "3")))
    outbreak_radius_km: float = field(default_factory=lambda: float(os.environ.get("AGRI_OUTBREAK_RADIUS_KM", "25")))
    outbreak_window_days: int = field(default_factory=lambda: int(os.environ.get("AGRI_OUTBREAK_WINDOW_DAYS", "14")))

    @property
    def llm_available(self) -> bool:
        return self.llm_enabled and bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
