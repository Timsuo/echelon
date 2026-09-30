from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Groups(StrictConfig):
    allowed: list[int] = Field(default_factory=list)

    @field_validator("allowed")
    @classmethod
    def valid_groups(cls, value: list[int]) -> list[int]:
        if any(group <= 0 for group in value):
            raise ValueError("群号必须为正整数")
        return list(dict.fromkeys(value))


class WebSocketConfig(StrictConfig):
    host: str = "127.0.0.1"
    port: int = Field(default=8789, ge=1, le=65535)
    action_timeout: float = Field(default=15, gt=0, le=120)


class DeepSeekConfig(StrictConfig):
    model: str = Field(default="deepseek-flash", min_length=1)
    thinking: bool = False
    timeout: float = Field(default=60, gt=0, le=600)
    retries: int = Field(default=2, ge=0, le=5)
    max_messages: int = Field(default=1000, ge=1, le=10000)
    max_input_chars: int = Field(default=60000, ge=1000, le=500000)


class LogConfig(StrictConfig):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class AppConfig(StrictConfig):
    groups: Groups = Field(default_factory=Groups)
    timezone: str = "Asia/Shanghai"
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    deepseek: DeepSeekConfig = Field(default_factory=DeepSeekConfig)
    logging: LogConfig = Field(default_factory=LogConfig)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8",
                                      extra="ignore", hide_input_in_errors=True)
    deepseek_api_key: SecretStr = SecretStr("")
    onebot_access_token: SecretStr
    admin_qq: int = Field(gt=0)

    @field_validator("onebot_access_token")
    @classmethod
    def nonempty_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("ONEBOT_ACCESS_TOKEN 不得为空")
        return value


def load_config(path: Path = ROOT / "config/config.yaml") -> AppConfig:
    with path.open(encoding="utf-8") as stream:
        return AppConfig.model_validate(yaml.safe_load(stream) or {})
