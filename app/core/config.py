from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    device_offline_max_days: int = 14

    ai_provider_api_key: str = ""
    ai_provider_base_url: str = ""
    ai_daily_call_cap: int = 200

    # Razorpay — left blank in dev; when unset the payments API reports the
    # gateway as unconfigured and the POS falls back to quick-tender buttons.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # WhatsApp Business Cloud API (Meta) — left blank in dev; campaign sending
    # reports "not configured" and refuses to send until these are set.
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_api_version: str = "v21.0"

    # Email (Resend — a single API key, no SMTP setup). Left blank in dev:
    # forgot-password falls back to flagging the request for an Admin to
    # resolve directly instead of emailing an OTP.
    resend_api_key: str = ""
    resend_from_email: str = "Gropto ERP <onboarding@resend.dev>"

    cors_origins: str = "http://localhost:5173"
    environment: str = "development"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
