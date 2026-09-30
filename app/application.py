import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from filelock import FileLock, Timeout

from app.commands.router import CommandRouter
from app.config import ROOT, AppConfig, Secrets, load_config
from app.jobs.service import SummaryService
from app.jobs.worker import run_worker
from app.llm.deepseek import DeepSeekClient
from app.logging_setup import configure_logging
from app.notifier.qq import run_notifier
from app.onebot.actions import ActionGateway
from app.onebot.events import EventProcessor
from app.onebot.server import router as websocket_router
from app.storage.db import Database
from app.storage.repository import Repository

logger = logging.getLogger(__name__)


@dataclass
class Services:
    secrets: Secrets
    repository: Repository
    actions: ActionGateway
    events: EventProcessor


def create_app(config: AppConfig | None = None, secrets: Secrets | None = None,
               root: Path = ROOT) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = config or load_config()
        credentials = secrets or Secrets()
        (root / "data").mkdir(parents=True, exist_ok=True)
        process_lock = FileLock(root / "data/monitor.lock")
        try:
            process_lock.acquire(timeout=0)
        except Timeout as error:
            raise RuntimeError("Monitor 已运行：拒绝启动第二实例") from error
        database = Database(root / "data/messages.db")
        client = None
        tasks: list[asyncio.Task] = []
        try:
            configure_logging(root / "logs", settings.logging.level, credentials)
            logger.info("Service starting")
            await database.open()
            repository = Repository(database)
            await repository.recover()
            started_at = time.time()
            await repository.state("start_time", str(started_at))
            actions = ActionGateway(credentials.admin_qq, settings.websocket.action_timeout)
            commands = CommandRouter(repository, settings, credentials.admin_qq, actions, started_at)
            events = EventProcessor(repository, settings.groups.allowed, commands)
            client = DeepSeekClient(settings.deepseek, credentials.deepseek_api_key.get_secret_value())
            service = SummaryService(repository, client, settings)
            app.state.services = Services(credentials, repository, actions, events)
            tasks = [asyncio.create_task(run_worker(repository, service), name="summary-worker"),
                     asyncio.create_task(run_notifier(repository, actions, credentials.admin_qq),
                                         name="private-notifier")]
            if not settings.groups.allowed:
                logger.warning("Group whitelist is empty; no group messages will be stored")
            if client.client is None:
                logger.warning("DeepSeek key missing; collection and status remain available")
            logger.info("Service ready")
            yield
        finally:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error("Background task exited: %s", type(result).__name__)
            try:
                if client:
                    await client.close()
            finally:
                try:
                    await database.close()
                finally:
                    process_lock.release()
            logger.info("Service stopped")

    app = FastAPI(title="QQ Monitor", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.include_router(websocket_router)
    return app
