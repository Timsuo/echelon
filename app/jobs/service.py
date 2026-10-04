import logging
import time

from app.config import AppConfig
from app.llm.deepseek import DeepSeekClient, SummaryError
from app.llm.prompts import transcript
from app.llm.schemas import SummaryData, add_topic_times
from app.storage.policy_repository import PolicyRepository
from app.storage.repository import Repository

logger = logging.getLogger(__name__)


class SummaryService:
    def __init__(self, repository: Repository, llm: DeepSeekClient, config: AppConfig) -> None:
        self.repository = repository
        self.llm = llm
        self.config = config

    async def execute(self, job: dict) -> None:
        logger.info("Summary job running id=%s", job["id"])
        try:
            if not isinstance(job.get("self_id"), int) or job["self_id"] <= 0:
                raise SummaryError("总结任务缺少有效 self_id")
            if not await self.repository.authorizations.is_active(job["self_id"], job["group_id"]):
                raise SummaryError("目标群已从白名单移除")
            policy = await PolicyRepository(self.repository.db).get(job["self_id"], job["group_id"])
            if policy.mode == "ignore" or not policy.summary_enabled:
                raise SummaryError("该群策略已暂停总结")
            messages = await self.repository.window_messages(
                job["self_id"], job["group_id"], job["window_start"], job["window_end"],
                self.config.deepseek.max_messages + 1)
            if len(messages) > self.config.deepseek.max_messages:
                raise SummaryError("消息数量超限，请缩短总结窗口")
            text = transcript(messages)
            if len(text) > self.config.deepseek.max_input_chars:
                raise SummaryError("聊天内容过长，请缩短总结窗口")
            if messages:
                previous = await self.repository.previous_summaries(job, self.config.summary)
                text = transcript(messages, previous)
                while previous and len(text) > self.config.deepseek.max_input_chars:
                    previous.pop()
                    text = transcript(messages, previous)
                try:
                    result = await self.llm.summarize(
                        text, lambda: self.repository.record_retry(job["id"]))
                    result = add_topic_times(result, messages)
                    known_ids = {row["message_id"] for row in messages}
                    if any(identifier not in known_ids for identifier in result.notable_message_ids):
                        raise SummaryError("模型引用了不存在的消息 ID")
                except SummaryError:
                    await self.repository.state("deepseek_health", "Failed")
                    raise
                await self.repository.state("deepseek_health", "Healthy")
                await self.repository.state("last_successful_api_call", str(time.time()))
            else:
                result = add_topic_times(SummaryData.empty(), [])
            await self.repository.complete_summary(job, self.config.deepseek.model, len(messages), result, self.config)
            logger.info("Summary job completed id=%s", job["id"])
        except Exception as error:
            safe_error = str(error) if isinstance(error, SummaryError) else type(error).__name__
            logger.error("Summary job failed id=%s reason=%s", job["id"], safe_error)
            await self.repository.fail_job(job, safe_error)
