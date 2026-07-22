from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, populated from environment variables.

    All variables are prefixed with SECONDTRACK_ (see .env.example).
    """

    model_config = SettingsConfigDict(
        env_prefix="SECONDTRACK_",
        # Read a local .env when present (dev). In Docker the real env vars
        # injected via compose `env_file:` take precedence; a missing file is fine.
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core
    secret_key: str = "dev-insecure-secret-change-me"
    admin_user: str = "admin"
    admin_password: str = "changeme"
    db_path: str = "./data/secondtrack.db"
    currency: str = "€"
    default_hourly_rate: float = 45.0
    cookie_secure: bool = False

    # Export
    export_dir: str = "./data/exports"

    # Uploaded files (project/part images, wallpaper).
    upload_dir: str = "./data/uploads"

    # Integrations
    woo_enabled: bool = False
    woo_url: str = ""
    woo_key: str = ""
    woo_secret: str = ""
    # Which Woo order statuses to surface in the hub (comma separated).
    woo_order_statuses: str = "processing,completed,on-hold"

    invoiceninja_enabled: bool = False
    invoiceninja_url: str = ""
    invoiceninja_token: str = ""
    # When creating an invoice, also email it to the customer immediately.
    invoiceninja_auto_send: bool = False

    # Vikunja (task tracking, Kanban view)
    vikunja_enabled: bool = False
    vikunja_url: str = ""
    vikunja_token: str = ""
    # Name of the parent project whose subprojects we surface (Kanban).
    vikunja_parent_project: str = "OpenVuture"

    # Nextcloud (WebDAV storage for invoice/receipt PDFs)
    nextcloud_enabled: bool = False
    nextcloud_url: str = ""
    nextcloud_user: str = ""
    # Use a Nextcloud *app password* (Settings → Security), not the login password.
    nextcloud_pass: str = ""
    # Base folder inside the user's files where documents are written
    # (Invoices/ and Expenses/ subfolders are created underneath).
    nextcloud_base_path: str = "/OpenVuture"
    # Auto-archive the invoice PDF to Nextcloud when an invoice is sent.
    nextcloud_auto_archive: bool = False

    # eBay (market-price suggestions for warehouse pricing)
    ebay_enabled: bool = False
    ebay_client_id: str = ""       # eBay developer App ID (Client ID)
    ebay_client_secret: str = ""   # eBay developer Cert ID (Client Secret)
    ebay_marketplace: str = "EBAY_DE"

    @property
    def woo_status_list(self) -> list[str]:
        return [s.strip() for s in self.woo_order_statuses.split(",") if s.strip()]

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
