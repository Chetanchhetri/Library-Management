from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    SECRET_KEY: str
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/library.db"

    ADMIN_EMAIL: str
    ADMIN_PASSWORD: str

    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""

    JWT_EXPIRE_MINUTES: int = 720
    DEV_ECHO_OTP: bool = False  # local testing only: log OTPs to console

    OTP_VALID_MINUTES: int = 10
    CHECKIN_WINDOW_MINUTES: int = 20
    RENEWAL_WINDOW_MINUTES: int = 5
    WORKER_INTERVAL_SECONDS: int = 5


settings = Settings()
