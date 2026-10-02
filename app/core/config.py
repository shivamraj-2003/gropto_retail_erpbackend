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
    # ERP assistant (/ai/chat). AI_PROVIDER_BASE_URL is the full endpoint, e.g.
    # https://api.openai.com/v1/chat/completions or https://api.anthropic.com/v1/messages.
    # AI_PROVIDER_KIND: "openai" (any OpenAI-compatible API) or "anthropic".
    ai_provider_kind: str = "openai"
    ai_model: str = ""
    ai_max_tokens: int = 600
    ai_timeout_seconds: int = 30

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

    # Point 19 hardening. MFA is mandatory for roles flagged roles.mfa_required
    # (and every Super Admin); set false only as an emergency break-glass.
    mfa_enforcement: bool = True
    # Separate small pool for reports/dashboards so heavy reads can't take the
    # connections POS billing needs. Main pool (5+5) + this (2+1) = 13, under
    # the Supabase pooler's 15-connection cap.
    reporting_pool_size: int = 2
    reporting_max_overflow: int = 1

    # Redis (Upstash). REDIS_URL (rediss://… TCP, TLS) is preferred — it backs
    # rate limiting, login lockout, cross-process permission-cache
    # invalidation, scheduler locks, OTP throttling and the Celery broker.
    # The REST pair is a fallback for the key-value features when only REST
    # is reachable (Celery needs TCP). All blank = single-process in-memory
    # mode, which is what this app ran on before.
    redis_url: str = ""
    upstash_redis_rest_url: str = ""
    upstash_redis_rest_token: str = ""
    redis_key_prefix: str = "gropto"
    # Notifications go through Celery workers when REDIS_URL is set; false
    # forces inline sending from the API process.
    celery_enabled: bool = True

    # OTP delivery (password reset, and any future verification flow).
    # SMS: MSG91 (auth key + approved OTP template) or Twilio (above);
    # "auto" uses whichever is configured, MSG91 first.
    sms_provider: str = "auto"
    msg91_auth_key: str = ""
    msg91_otp_template_id: str = ""
    msg91_sender_id: str = ""
    # Meta-approved WhatsApp authentication template with one body variable
    # (the code) — Meta requires a template for OTPs.
    whatsapp_otp_template: str = "otp_code"
    whatsapp_otp_language: str = "en_US"
    otp_expiry_minutes: int = 10
    otp_resend_cooldown_seconds: int = 60
    otp_max_sends_per_hour: int = 5

    # Gunicorn workers for the Docker image (one APScheduler runs per worker,
    # but each job takes a Redis lock, so only one worker executes it).
    web_concurrency: int = 2

    cors_origins: str = "http://localhost:5173"
    environment: str = "development"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
