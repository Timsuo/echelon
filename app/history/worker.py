import asyncio
import logging
import time

from app.onebot.actions import ActionRejectedError
from app.onebot.events import EventProcessor
from app.onebot.history import HistoryAdapter
from app.operations.health import beat
from app.storage.history_repository import HistoryRepository
from app.storage.policy_repository import PolicyRepository

logger = logging.getLogger(__name__)


def evaluate_coverage(oldest: int | None, newest: int | None, start: float, end: float, invalid: int = 0) -> str:
    if oldest is None or newest is None:
        return "unknown"
    if oldest <= start and newest >= end and not invalid:
        return "likely_covered"
    return "partial"


class HistoryWorker:
    def __init__(self, repository: HistoryRepository, adapter: HistoryAdapter, events: EventProcessor) -> None:
        self.repository = repository
        self.adapter = adapter
        self.events = events
        self.config = repository.config

    async def execute(self, job: dict) -> None:
        received = inserted = invalid = 0
        oldest = newest = None
        try:
            active_job = await self.repository.repository.query("SELECT id FROM history_sync_jobs WHERE id=? AND self_id=? AND status='running' AND attempts=?", (job['id'], job['self_id'], job['attempts']))
            if not active_job:
                raise PermissionError('History job no longer running')
            version = await self.repository.repository.authorizations.version(job['self_id'], job['group_id'])
            if not self.config.history.enabled or not await self.repository.repository.authorizations.is_active(job["self_id"], job["group_id"]):
                raise PermissionError("History disabled or group removed")
            policy = await PolicyRepository(self.repository.db).get(job["self_id"], job["group_id"])
            if policy.mode == "ignore":
                raise PermissionError("Group ignored")
            if job["attempts"] > self.config.history.retry_count + 1:
                raise PermissionError("History retry budget exhausted after interruption")
            count = self.config.history.reconnect_count if job["mode"] == "reconnect" else self.config.history.periodic_count
            batch = await self.adapter.fetch(job["self_id"], job["group_id"], count, time.time())
            if version != await self.repository.repository.authorizations.version(job['self_id'], job['group_id']):
                raise PermissionError('History authorization changed during request')
            if await self.repository.repository.state("onebot_self_id") != str(job["self_id"]):
                raise PermissionError("History account changed")
            received, invalid = batch.received, batch.invalid
            times = [message["time"] for message in batch.messages]
            oldest, newest = (min(times), max(times)) if times else (None, None)
            overlap = self.config.history.overlap_seconds
            source = "history_poll" if job["mode"] == "periodic" else "history_recovery"
            for message in batch.messages:
                if max(0, job["window_start"] - overlap) <= message["time"] <= job["window_end"] + overlap:
                    try:
                        # Original event_time remains intact. Future alerts must not use backfill received_time.
                        inserted += await self.events.handle(message, ingest_source=source, authorization_version=version)
                    except (ValueError, TypeError, KeyError):
                        invalid += 1
                        logger.warning("History message normalization rejected job=%s", job["id"])
            policy = await PolicyRepository(self.repository.db).get(job["self_id"], job["group_id"])
            if policy.mode == "ignore":
                raise PermissionError("Group policy changed during history ingestion")
            coverage = evaluate_coverage(oldest, newest, job["window_start"], job["window_end"], invalid)
            await self.repository.finish(job, coverage, received, inserted, invalid, oldest, newest)
            logger.info("History sync job=%s mode=%s fetched=%s inserted=%s coverage=%s", job["id"], job["mode"], received, inserted, coverage)
        except Exception as error:
            retryable = isinstance(error, (ConnectionError, TimeoutError, ActionRejectedError))
            retry = retryable and job["attempts"] <= self.config.history.retry_count
            await self.repository.finish(job, "failed", received, inserted, invalid, oldest, newest,
                                         error=type(error).__name__, retry=retry)
            logger.warning("History sync failed job=%s kind=%s retry=%s", job["id"], type(error).__name__, retry)

    async def run(self) -> None:
        next_schedule = 0.0
        while True:
            try:
                actions = self.adapter.actions
                if not actions.connected or actions.self_id is None:
                    await beat(self.repository.db, 'history')
                    await asyncio.sleep(1)
                    continue
                if time.monotonic() >= next_schedule:
                    await self.repository.schedule(actions.self_id)
                    next_schedule = time.monotonic() + 30
                job = await self.repository.claim(actions.self_id)
                if job:
                    await self.execute(job)
                await self.repository.report_ready(actions.self_id)
                await beat(self.repository.db, 'history')
                await asyncio.sleep(self.config.history.request_interval_seconds if job else 1)
            except asyncio.CancelledError:
                logger.info("History worker stopped; interrupted jobs resume on startup")
                raise
            except Exception as error:
                logger.error("History worker failure: %s", type(error).__name__)
                await asyncio.sleep(5)
