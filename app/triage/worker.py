import asyncio
import logging

from app.llm.deepseek import DeepSeekClient, RetryableModelOutputError, retryable
from app.storage.history_repository import HistoryRepository
from app.storage.policy_repository import PolicyRepository
from app.storage.triage_repository import TriageRepository
from app.triage.deadlines import validate_deadlines
from app.triage.prompts import TRIAGE_PROMPT, triage_input

logger = logging.getLogger(__name__)


class TriageWorker:
    def __init__(self, repository: TriageRepository, llm: DeepSeekClient) -> None:
        self.repository, self.llm = repository, llm

    async def execute(self, job: dict) -> None:
        repo, config = self.repository, self.repository.config
        try:
            if not config.triage.enabled or job['attempts'] > config.triage.retry_count + 1:
                raise PermissionError("Triage disabled or retry budget exhausted")
            policy = await PolicyRepository(repo.db, config.groups.allowed).get(job['self_id'], job['group_id'])
            if policy.mode not in {'inbox', 'priority'} or not policy.inbox_enabled:
                raise PermissionError("Triage policy paused")
            if await repo.repository.state('onebot_self_id') != str(job['self_id']):
                raise PermissionError("Triage account changed")
            messages, attachments, candidates = await repo.context(job)
            preferences = await repo.preferences(job['self_id'])
            gaps = await HistoryRepository(repo.repository, config).unresolved(
                job['self_id'], job['group_id'], job['window_start'], job['window_end'])
            text = triage_input(messages, attachments, candidates, policy.model_dump(), preferences.model_dump(), config.timezone)
            # Recent merge context is optional; discard oldest candidates before rejecting source data.
            while candidates and len(text) + len(TRIAGE_PROMPT) > config.triage.max_input_chars:
                candidates.pop()
                text = triage_input(messages, attachments, candidates, policy.model_dump(), preferences.model_dump(), config.timezone)
            if len(text) + len(TRIAGE_PROMPT) > config.triage.max_input_chars:
                raise ValueError("Triage input exceeds configured limit")
            result = await self.llm.triage(text)
            try:
                result.validate_references({m['id'] for m in messages}, {c['id'] for c in candidates})
                validate_deadlines(result, messages, config.timezone)
            except ValueError as error:
                raise RetryableModelOutputError("Triage reference validation failed") from error
            await repo.apply(job, result, candidates, bool(gaps), config.deepseek.model)
        except Exception as error:
            # No traceback, input, provider body or model output reaches logs/outbox.
            again = retryable(error) or (error.__cause__ is not None and retryable(error.__cause__))
            await repo.fail(job, type(error).__name__, again)
            logger.warning("Triage failed job=%s error_class=%s retry=%s", job['id'], type(error).__name__, again)

    async def run(self) -> None:
        while True:
            try:
                job = await self.repository.claim()
                if job:
                    await self.execute(job)
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info("Triage worker stopped; ownership retained for restart")
                raise
            except Exception as error:
                logger.error("Triage worker failure error_class=%s", type(error).__name__)
                await asyncio.sleep(3)
