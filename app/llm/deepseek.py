import logging
from collections.abc import Awaitable, Callable

from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from pydantic import BaseModel, ValidationError
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import DeepSeekConfig
from app.llm.prompts import SYSTEM_PROMPT
from app.llm.schemas import SummaryData
from app.policies.models import ConfigIntent
from app.policies.parser import SYSTEM_PROMPT as CONFIG_PROMPT
from app.triage.models import PreferenceIntent, TriageResult
from app.triage.prompts import PREFERENCE_PROMPT, TRIAGE_PROMPT

logger = logging.getLogger(__name__)
RETRYABLE_FINISH_REASONS = frozenset({"insufficient_system_resource", "aborted"})


class SummaryError(Exception):
    """Safe-to-display error, never contains provider response bodies or credentials."""


class RetryableModelOutputError(SummaryError):
    """Temporary model output failure; subject to the same bounded retry budget."""


class NonRetryableModelOutputError(SummaryError):
    """Repeating the identical request is not expected to fix this output."""


def retryable(error: BaseException) -> bool:
    return isinstance(error, (APIConnectionError, RetryableModelOutputError)) or (
        isinstance(error, APIStatusError) and
        (error.status_code in {408, 409, 429} or error.status_code >= 500))


class DeepSeekClient:
    def __init__(self, config: DeepSeekConfig, api_key: str) -> None:
        self.config = config
        self.client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com",
                                  timeout=config.timeout, max_retries=0) if api_key else None

    async def summarize(self, text: str, on_retry: Callable[[], Awaitable[None]]) -> SummaryData:
        return await self._generate(text, SYSTEM_PROMPT, SummaryData, on_retry)

    async def parse_config(self, text: str, on_retry: Callable[[], Awaitable[None]]) -> ConfigIntent:
        return await self._generate(text, CONFIG_PROMPT, ConfigIntent, on_retry)

    async def parse_preferences(self, text: str, on_retry: Callable[[], Awaitable[None]]) -> PreferenceIntent:
        return await self._generate(text, PREFERENCE_PROMPT, PreferenceIntent, on_retry)

    async def triage(self, text: str) -> TriageResult:
        # Persistent job owns the retry budget; do not multiply it by SDK/output retries.
        async def no_retry() -> None:
            return None
        return await self._generate(text, TRIAGE_PROMPT, TriageResult, no_retry, retries=0)

    async def _generate[T: BaseModel](self, text: str, system: str, schema: type[T],
                                       on_retry: Callable[[], Awaitable[None]], *, retries: int | None = None) -> T:
        if self.client is None:
            raise SummaryError("未配置 DEEPSEEK_API_KEY")
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt((self.config.retries if retries is None else retries) + 1),
                wait=wait_exponential(multiplier=1, min=1, max=30),
                retry=retry_if_exception(retryable), reraise=True,
            ):
                with attempt:
                    if attempt.retry_state.attempt_number > 1:
                        await on_retry()
                        logger.warning("DeepSeek retry %s/%s", attempt.retry_state.attempt_number - 1,
                                       self.config.retries)
                    logger.info("DeepSeek request attempt=%s", attempt.retry_state.attempt_number)
                    response = await self.client.chat.completions.create(
                        model=self.config.model,
                        messages=[{"role": "system", "content": system},
                                  {"role": "user", "content": text}],
                        response_format={"type": "json_object"}, max_tokens=8000,
                        extra_body={"thinking": {"type": "enabled" if self.config.thinking else "disabled"}},
                    )
                    if not response.choices:
                        logger.warning("DeepSeek returned no choices")
                        raise RetryableModelOutputError("模型返回空 choices")
                    reason = response.choices[0].finish_reason
                    if reason == "length":
                        raise NonRetryableModelOutputError("模型输出超出 token 限制，请缩短 summary 时间窗口")
                    if reason in RETRYABLE_FINISH_REASONS:
                        logger.warning("DeepSeek transient finish_reason=%s", reason)
                        raise RetryableModelOutputError("模型生成被临时中断")
                    if reason != "stop":
                        # Unknown reasons fail closed, without logging arbitrary provider strings.
                        raise NonRetryableModelOutputError("模型以非预期原因结束，未接受其输出")
                    raw = response.choices[0].message.content or ""
                    if not raw.strip():
                        logger.warning("DeepSeek returned empty content")
                        raise RetryableModelOutputError("模型返回空内容")
                    try:
                        result = schema.model_validate_json(raw)
                    except ValidationError as error:
                        logger.error("DeepSeek schema validation failed schema=%s response_length=%s "
                                     "finish_reason=stop error_class=%s", schema.__name__, len(raw), type(error).__name__)
                        raise RetryableModelOutputError("模型返回 JSON 不合法或不符合 Schema") from error
                    logger.info("DeepSeek API request succeeded")
                    return result
        except SummaryError as error:
            logger.error("DeepSeek output failed: %s", type(error).__name__)
            raise
        except Exception as error:
            code = error.status_code if isinstance(error, APIStatusError) else None
            logger.error("DeepSeek API failed kind=%s status=%s", type(error).__name__, code)
            raise SummaryError(f"DeepSeek 请求失败（{type(error).__name__}，HTTP {code}）") from error
        raise SummaryError("DeepSeek 未返回结果")

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
