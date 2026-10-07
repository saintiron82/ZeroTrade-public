"""FF Trading Bot — Configuration."""

from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


class KISConfig(BaseSettings):
    app_key: str = Field(default="", alias="KIS_APP_KEY")
    app_secret: str = Field(default="", alias="KIS_APP_SECRET")
    account_no: str = Field(default="00000000-01", alias="KIS_ACCOUNT_NO")
    is_paper: bool = Field(default=True, alias="KIS_IS_PAPER")

    # 실전투자 키 (시세/잔고 조회용)
    real_app_key: str = Field(default="", alias="KIS_REAL_APP_KEY")
    real_app_secret: str = Field(default="", alias="KIS_REAL_APP_SECRET")
    real_account_no: str = Field(default="", alias="KIS_REAL_ACCOUNT_NO")

    @property
    def real_account_prefix(self) -> str:
        return self.real_account_no.split("-")[0] if self.real_account_no else self.account_prefix

    @property
    def real_account_suffix(self) -> str:
        parts = self.real_account_no.split("-")
        return parts[1] if len(parts) > 1 else "01"

    @property
    def base_url(self) -> str:
        if self.is_paper:
            return "https://openapivts.koreainvestment.com:29443"
        return "https://openapi.koreainvestment.com:9443"

    @property
    def ws_url(self) -> str:
        if self.is_paper:
            return "ws://ops.koreainvestment.com:31000"
        return "ws://ops.koreainvestment.com:21000"

    @property
    def account_prefix(self) -> str:
        return self.account_no.split("-")[0]

    @property
    def account_suffix(self) -> str:
        parts = self.account_no.split("-")
        return parts[1] if len(parts) > 1 else "01"


class NotifyConfig(BaseSettings):
    slack_webhook_url: str = Field(default="", alias="SLACK_WEBHOOK_URL")
    discord_webhook_url: str = Field(default="", alias="DISCORD_WEBHOOK_URL")


class Settings(BaseSettings):
    kis: KISConfig = Field(default_factory=KISConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)

    conditions_dir: Path = PROJECT_ROOT / "conditions"
    logs_dir: Path = PROJECT_ROOT / "logs"

    def ensure_dirs(self):
        self.conditions_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)


settings = Settings()
