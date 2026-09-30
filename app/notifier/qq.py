import asyncio
import logging

from app.onebot.actions import ActionGateway
from app.storage.repository import Repository

logger = logging.getLogger(__name__)


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
            try:
                await actions.call("send_private_msg", {"user_id": admin_qq, "text": item["text"]})
            except Exception as error:
                await repository.notification_result(item, type(error).__name__)
            else:
                await repository.notification_result(item)
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            logger.info("Private notifier stopped")
            raise
        except Exception as error:
            logger.error("Private notifier failed: %s", type(error).__name__)
            await asyncio.sleep(2)
