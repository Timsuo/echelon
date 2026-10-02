import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class DeliveryWorker:
    def __init__(self, repository):
        self.repository = repository

    async def run(self):
        due = 0
        while True:
            try:
                sid = await self.repository.repository.state('onebot_self_id')
                if sid and time.monotonic() >= due:
                    interval = await self.repository.tick(int(sid))
                    due = time.monotonic() + interval
                elif sid:
                    # Manual digests are background work, but don't wait a whole heartbeat.
                    async with self.repository.db.transaction() as connection:
                        from app.delivery.preferences import read_delivery_preferences
                        prefs = await read_delivery_preferences(connection, int(sid))
                        await self.repository.digest(connection, int(sid), prefs, time.time())
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error('Delivery heartbeat failed: %s', type(error).__name__)
                await asyncio.sleep(5)
