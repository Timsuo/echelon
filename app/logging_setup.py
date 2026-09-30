import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import Secrets


class SecretFormatter(logging.Formatter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.secrets = sorted({variant for secret in secrets if secret for variant in (
            secret, repr(secret)[1:-1], json.dumps(secret)[1:-1])}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        result = super().format(record)
        for secret in self.secrets:
            result = result.replace(secret, "[REDACTED]")
        return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", result)


def configure_logging(directory: Path, level: str, secrets: Secrets) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    formatter = SecretFormatter([secrets.deepseek_api_key.get_secret_value(),
                                 secrets.onebot_access_token.get_secret_value()])
    handlers = [logging.StreamHandler(), RotatingFileHandler(
        directory / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")]
    root = logging.getLogger()
    for old in root.handlers[:]:
        root.removeHandler(old)
        old.close()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(level)
    for name in ("httpx", "httpcore", "openai", "aiosqlite", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Prevent uvicorn from logging a handshake URL containing a query token.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
