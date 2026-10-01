from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
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

    # SMS — Twilio. Left blank in dev; same is_configured() gate as every
    # other provider in this file (campaign sends report "not configured"
    # per-recipient rather than fabricating a delivery).
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # Push — Firebase Cloud Messaging (legacy HTTP server-key API, simplest
    # to configure without a full service-account JSON). Left blank in dev.
    fcm_server_key: str = ""

    # Email (Resend — a single API key, no SMTP setup). Left blank in dev:
    # forgot-password falls back to flagging the request for an Admin to
    # resolve directly instead of emailing an OTP.
    resend_api_key: str = ""
    resend_from_email: str = "Gropto ERP <onboarding@resend.dev>"

    # GST e-invoice / IRP (Invoice Registration Portal) via a GSP (GST
    # Suvidha Provider) — left blank in dev; when unset, e-invoice submission
    # reports "not configured" and the invoice sits in `pending` status for
    # manual/later submission rather than fabricating an IRN. Same pattern as
    # razorpay_key_id above.
    gsp_api_base_url: str = ""
    gsp_client_id: str = ""
    gsp_client_secret: str = ""
    gsp_gstin: str = ""

    cors_origins: str = "http://localhost:5173"
    environment: str = "development"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
