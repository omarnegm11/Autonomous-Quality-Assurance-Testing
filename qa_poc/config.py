from pathlib import Path
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    test_database_url: SecretStr
    agent_state_database_url: SecretStr
    verifier_database_url: SecretStr
    test_user_a_password: SecretStr
    test_user_b_password: SecretStr
    llm_provider: str = "groq"
    llm_model: str = "qwen/qwen3.8-27b"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = ""
    llm_reasoning_effort: str = ""
    llm_rate_limit_retries: int = Field(default=3, ge=0, le=10)
    llm_max_retry_wait_seconds: int = Field(default=60, ge=1, le=600)
    app_url: str = "http://127.0.0.1:8000"
    app_source: Path = Path(".local/builds/clean")
    runs_dir: Path = Path("runs")
    browsers_path: Path = Path(".local/browsers")
    browser_headless: bool = True
    browser_slow_mo_ms: int = Field(default=120, ge=0, le=2000)
    browser_type_delay_ms: int = Field(default=20, ge=0, le=100)
    browser_max_restarts: int = Field(default=2, ge=0, le=5)
    specialist_actions_per_activation: int = Field(default=1, ge=1, le=6)
    max_actions: int = 60
    max_seconds: int = 300
    max_model_tokens: int = 30000
    random_seed: int = 7
    api_url: str = ""
    api_spec_url: str = ""
    api_auth_header: str = "Authorization"
    api_auth_value: SecretStr = SecretStr("")
    requirements: str = ""
    workflow: str = "task_lifecycle"
    dashboard_run_id: str = ""
    stop_file: Path | None = None
    live_stream_dir: Path | None = None

    @model_validator(mode="after")
    def separate_databases(self):
        app, state, verifier = [make_url(v.get_secret_value()) for v in (
            self.test_database_url, self.agent_state_database_url, self.verifier_database_url)]
        if any(u.get_backend_name() != "postgresql" for u in (app, state, verifier)):
            raise ValueError("This PoC requires PostgreSQL for both logical stores")
        if (app.host, app.port, app.database) == (state.host, state.port, state.database):
            raise ValueError("Application and agent state must use separate databases")
        if (app.host, app.port, app.database) != (verifier.host, verifier.port, verifier.database):
            raise ValueError("Verifier must read the same application test database")
        if app.username == verifier.username:
            raise ValueError("Verifier must use a separate read-only database role")
        if not all(1 <= n for n in (self.max_actions, self.max_seconds, self.max_model_tokens)):
            raise ValueError("Budgets must be positive")
        return self

    def password(self, identity: str) -> str:
        if identity not in ("user_a", "user_b"):
            raise ValueError("Unknown test identity")
        return getattr(self, f"test_{identity}_password").get_secret_value()
