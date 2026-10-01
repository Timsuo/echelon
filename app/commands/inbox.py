import logging
import re

from app.attachments.storage import AttachmentStorage
from app.config import AppConfig
from app.inbox.renderer import render_detail, render_inbox
from app.onebot.adapter import MessageEvent
from app.storage.inbox_repository import InboxRepository
from app.storage.repository import Repository

logger = logging.getLogger(__name__)


class InboxCommands:
    def __init__(self, repository: Repository, config: AppConfig,
                 storage: AttachmentStorage | None) -> None:
        self.repository = repository
        self.inbox = InboxRepository(repository.db)
        self.config = config
        self.storage = storage

    async def account(self, event: MessageEvent) -> int:
        if await self.repository.state("onebot_self_id") != str(event.self_id):
            raise ValueError("当前机器人账号不匹配")
        return event.self_id

    @staticmethod
    def identifiers(argument: str, count: int) -> list[int]:
        parts = argument.split()
        if len(parts) != count or any(not re.fullmatch(r"[1-9][0-9]{0,17}", part) for part in parts):
            raise ValueError("参数必须是正整数 ID / 附件序号，不能使用文件路径。")
        return [int(part) for part in parts]

    async def listing(self, event: MessageEvent, argument: str) -> None:
        self_id = await self.account(event)
        if argument not in {"", "unread"}:
            raise ValueError("用法：/inbox 或 /inbox unread")
        items = await self.inbox.list_items(self_id, argument == "unread", self.config.inbox.default_page_size)
        await self.repository.notify(render_inbox(items, await self.inbox.unread_count(self_id),
                                                  self.config.timezone), self_id)

    async def detail(self, event: MessageEvent, argument: str) -> None:
        self_id = await self.account(event)
        item_id, = self.identifiers(argument, 1)
        item = await self.inbox.detail(self_id, item_id, mark_read=True)
        if item is None:
            raise ValueError("收件箱条目不存在。")
        await self.repository.notify(render_detail(item, self.config.timezone), self_id)
        logger.info("Inbox item opened id=%s", item_id)

    async def archive(self, event: MessageEvent, argument: str) -> None:
        self_id = await self.account(event)
        item_id, = self.identifiers(argument, 1)
        if not await self.inbox.archive(self_id, item_id):
            raise ValueError("收件箱条目不存在。")
        await self.repository.notify(f"Echelon #{item_id} 已归档。", self_id)
        logger.info("Inbox item archived id=%s", item_id)

    async def file(self, event: MessageEvent, argument: str) -> None:
        self_id = await self.account(event)
        item_id, index = self.identifiers(argument, 2)
        item = await self.inbox.detail(self_id, item_id)
        if item is None:
            raise ValueError("收件箱条目不存在。")
        if index > len(item["attachments"]):
            raise ValueError("附件序号不存在。")
        attachment = item["attachments"][index - 1]
        if attachment["error"] == "size_limit":
            raise ValueError("附件超过自动下载大小限制。")
        if attachment["download_status"] != "downloaded":
            raise ValueError("附件尚未准备完成。")
        if not self.config.attachments.enabled or self.storage is None:
            raise ValueError("附件功能已关闭。")
        try:
            self.storage.validate(attachment)
        except (OSError, PermissionError):
            raise ValueError("附件路径或文件状态不安全，已拒绝发送。") from None
        await self.repository.enqueue_file(self_id, attachment["id"])
