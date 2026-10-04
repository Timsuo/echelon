import asyncio
import logging

from app.llm.deepseek import DeepSeekClient, InvalidModelSchemaError, SummaryError
from app.operations.health import beat
from app.policies.errors import ConfigErrorCode, ConfigParseError
from app.policies.models import ConfigFeedback
from app.policies.parser import ConfigIntentParser
from app.storage.policy_repository import PolicyRepository
from app.storage.preference_repository import PreferenceRepository

logger = logging.getLogger(__name__)


class ConfigurationWorker:
    def __init__(self, repository: PolicyRepository, llm: DeepSeekClient, actions=None) -> None:
        self.repository = repository
        self.llm = llm
        self.actions = actions

    async def execute(self, request: dict) -> None:
        try:
            if request.get('kind') == 'group_authorization':
                from app.commands.authorization import verify_request
                await verify_request(self.repository, self.actions, request)
                return
            if request.get('kind') == 'delivery_preferences':
                from app.delivery.preferences import DeliveryPreferenceRepository
                intent = await self.llm.parse_delivery_preferences(request['input_text'], lambda: self.repository.retry(request['id']))
                await DeliveryPreferenceRepository(self.repository).propose(request, intent)
                return
            if request.get('kind') == 'triage_preferences':
                intent = await self.llm.parse_preferences(request['input_text'], lambda: self.repository.retry(request['id']))
                await PreferenceRepository(self.repository).propose(request, intent)
                return
            body = await self.repository.config_body(request)
            if body is None:
                return
            intent = await self.llm.parse_config(body, lambda: self.repository.retry(request["id"]))
            ConfigIntentParser.validate_intent(intent)
            if isinstance(intent, ConfigFeedback):
                await self.repository.complete_feedback(request, intent)
                return
            await self.repository.propose(request["self_id"], request["admin_qq"], request["target_group_id"], intent, request["id"])
        except Exception as error:
            # Never echo raw model/user input or validation internals to logs/outbox.
            if request.get('kind', 'group_policy') == 'group_policy':
                code = (error.code if isinstance(error, ConfigParseError) else
                        error.config_code if isinstance(error, InvalidModelSchemaError) else ConfigErrorCode.PROVIDER_ERROR)
                safe = f"[{code.value}] {ConfigParseError(code)}"
                logger.warning("Configuration parsing failed id=%s code=%s kind=%s", request["id"], code.value, type(error).__name__)
            else:
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
                await beat(self.repository.db, 'configuration')
            except asyncio.CancelledError:
                logger.info("Configuration worker stopped")
                raise
            except Exception as error:
                logger.error("Configuration worker failure: %s", type(error).__name__)
                await asyncio.sleep(2)
