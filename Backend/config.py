from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Rakt Doot"
    environment: str = "local"              # local | staging | production
    # NBTC guidance as we understand it (confirm with a blood centre): 3 months for men, 4 months for women.
    donation_interval_male_days: int = 90
    donation_interval_female_days: int = 120        # also for 'other' and unknown sex: the safe side

    database_url: str                       # required: no default, so a missing value fails fast
    # The OWNER account, used by Alembic only. The app itself connects with database_url as <db>_app (migration 0008).
    # Unset = migrations use database_url (fine before 0008 exists on that database, never in production).
    migrations_database_url: str | None = None
    app_db_password: str | None = Field(default=None, repr=False)   # manage.py migrate sets the app role's password
    test_database_url: str | None = None    # tests only (owner account); must point at a *_test database
    pool_size: int = 5
    max_overflow: int = 5
    allocation_radius_km: float = 50        # how far to look for blood banks when allocating
    # Holds (ADR 021). Product defaults, i.e. guesses to tune with real banks, not clinical or legal limits.
    hold_confirm_minutes: int = 30          # a PENDING hold the bank has not confirmed is released after this
    hold_pickup_hours: int = 6              # CONFIRMED and not collected by then: OVERDUE (never auto-released)
    hold_expiry_interval_seconds: float = 30    # how often the worker looks for expired holds
    max_held_units_per_hospital: int = 20   # anti-hoarding: live held units per hospital across its open requests
    sse_heartbeat_seconds: float = 15       # an idle live stream sends a comment this often
    sql_echo: bool = False                  # True prints every SQL statement (learning/debugging)
    # browser origins allowed to call the API (the Next.js app). Env form: LIFELINK_CORS_ORIGINS='["https://app.example"]'
    cors_origins: list[str] = ["http://localhost:3000"]

    # ---------- Stage 2: identity ----------
    jwt_secret: str = Field(min_length=32, repr=False)  # required; repr=False keeps it out of logs and tracebacks
    access_token_minutes: int = 15          # how long a stolen access token stays useful (no revocation list yet)
    refresh_token_days: int = 14
    cookie_secure: bool = True              # secure by default; plain-http local dev sets it false in .env
    max_failed_logins: int = 5              # then the account locks for lockout_minutes
    lockout_minutes: int = 15
    auth_rate_limit_per_minute: int = 10    # per client IP, on login / register (not refresh: see auth router)
    public_rate_limit_per_minute: int = Field(60, gt=0)  # per client IP, sustained refill on /v1/public/... (token bucket)
    public_rate_burst: int = Field(20, ge=1)             # per client IP, searches allowed back to back

    model_config = SettingsConfigDict(env_file=".env", env_prefix="LIFELINK_", extra="ignore")

    @model_validator(mode="after")
    def no_insecure_cookies_in_production(self):
        if self.environment == "production" and not self.cookie_secure:
            raise ValueError("cookie_secure must be true in production: refresh tokens would travel over plain http")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
