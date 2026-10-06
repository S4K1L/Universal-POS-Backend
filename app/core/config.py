from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "change-me-to-a-long-random-string-of-at-least-32-chars"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[2] / ".env", extra="ignore")  # backend/.env, whatever the cwd

    env: str = "development"
    database_url: str = "postgresql+asyncpg://localhost/universalpos"
    jwt_secret_key: str = _DEV_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 30
    cors_origins: str = "*"
    upload_dir: str = "uploads"
    max_upload_mb: int = 5

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def upload_root(self) -> Path:
        return Path(self.upload_dir).resolve()

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    def assert_production_safe(self) -> None:
        """Refuse to boot a production instance with development defaults."""
        if not self.is_production:
            return
        problems = []
        if self.jwt_secret_key == _DEV_SECRET or len(self.jwt_secret_key) < 32:
            problems.append("JWT_SECRET_KEY must be set to a random string of at least 32 characters")
        if "*" in self.cors_origin_list:
            problems.append("CORS_ORIGINS must list explicit origins, not '*'")
        if problems:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


settings = Settings()
