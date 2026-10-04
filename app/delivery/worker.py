import asyncio
import logging
import time

from app.operations.health import beat

logger = logging.getLogger(__name__)


class DeliveryWorker:
    def __init__(self, repository):
        self.repository = repository

    async def run(self):
        db = self.repository.db
        due = 0
        while True:
            try:
                sid = await self.repository.repository.state('onebot_self_id')
                if sid and (db.delivery_wakeup.is_set() or time.time() >= due):
                    # Clear before reading SQLite: concurrent wakeups during the
                    # transaction remain set; Event is only a latency hint.
                    db.delivery_wakeup.clear()
                    interval = await self.repository.tick(int(sid))
                    due = await self.repository.next_due(int(sid), time.time()+interval)
                await beat(db, 'delivery')
                delay = min(30, max(0.1, due-time.time())) if sid else 1
                if not sid:
                    db.delivery_wakeup.clear()
                try:
                    await asyncio.wait_for(db.delivery_wakeup.wait(), timeout=delay)
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error('Delivery heartbeat failed: %s', type(error).__name__)
                await asyncio.sleep(5)
