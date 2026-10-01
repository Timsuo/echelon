import asyncio
import logging

from app.onebot.actions import ActionGateway
from app.storage.repository import Repository

logger = logging.getLogger(__name__)


async def deliver_notification(repository: Repository, actions: ActionGateway, admin_qq: int, item: dict) -> None:
    is_file = item.get("kind") == "file"
    try:
        if is_file:
            await actions.call("upload_private_file", {"user_id": admin_qq, "self_id": item["self_id"],
                                                       "attachment_id": item["attachment_id"]})
        else:
            await actions.call("send_private_msg", {"user_id": admin_qq, "text": item["text"]},
                               expected_self_id=item.get("self_id"))
    except Exception as error:
        if is_file:
            logger.warning("Private file delivery failed attachment_id=%s kind=%s", item["attachment_id"], type(error).__name__)
        if is_file and (isinstance(error, (PermissionError, FileNotFoundError, ValueError)) or item["attempts"] >= 2):
            await repository.cancel_notification(item, type(error).__name__)
        else:
            await repository.notification_result(item, type(error).__name__)
    else:
        await repository.notification_result(item)
        if is_file:
            logger.info("Private file delivered attachment_id=%s", item["attachment_id"])


async def run_notifier(repository: Repository, actions: ActionGateway, admin_qq: int) -> None:
    while True:
        try:
            if not actions.connected:
                await asyncio.sleep(1)
                continue
            item = await repository.next_notification()
            if item is None:
                await asyncio.sleep(0.5)
                continue
            await deliver_notification(repository, actions, admin_qq, item)
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            logger.info("Private notifier stopped")
            raise
        except Exception as error:
            logger.error("Private notifier failed: %s", type(error).__name__)
            await asyncio.sleep(2)
