import asyncio
import logging

from app.llm.deepseek import DeepSeekClient, SummaryError
from app.policies.parser import ConfigIntentParser
from app.storage.policy_repository import PolicyRepository
from app.storage.preference_repository import PreferenceRepository

logger = logging.getLogger(__name__)


class ConfigurationWorker:
    def __init__(self, repository: PolicyRepository, llm: DeepSeekClient) -> None:
        self.repository = repository
        self.llm = llm

    async def execute(self, request: dict) -> None:
        try:
            if request.get('kind') == 'triage_preferences':
                intent = await self.llm.parse_preferences(request['input_text'], lambda: self.repository.retry(request['id']))
                await PreferenceRepository(self.repository).propose(request, intent)
                return
            self.repository.check_group(request["group_id"])
            intent = await self.llm.parse_config(request["input_text"], lambda: self.repository.retry(request["id"]))
            ConfigIntentParser.validate_intent(intent, request["input_text"])
            await self.repository.propose(request["self_id"], request["admin_qq"], request["group_id"], intent, request["id"])
        except Exception as error:
            # Never echo raw model/user input or validation internals to logs/outbox.
            safe = str(error) if isinstance(error, SummaryError) else type(error).__name__
            logger.warning("Configuration parsing failed id=%s kind=%s", request["id"], type(error).__name__)
            await self.repository.failed(request, safe)

    async def run(self) -> None:
        while True:
            try:
                request = await self.repository.claim()
                if request:
                    await self.execute(request)
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info("Configuration worker stopped")
                raise
            except Exception as error:
                logger.error("Configuration worker failure: %s", type(error).__name__)
                await asyncio.sleep(2)
