import asyncio
import logging

from app.jobs.service import SummaryService
from app.operations.health import beat
from app.storage.repository import Repository

logger = logging.getLogger(__name__)


async def run_worker(repository: Repository, service: SummaryService) -> None:
    while True:
        job = None
        try:
            job = await repository.claim_job()
            if job:
                await service.execute(job)
            else:
                await asyncio.sleep(1)
            await beat(repository.db, 'summary')
        except asyncio.CancelledError:
            logger.info("Summary worker stopped; interrupted job recovered at next startup")
            raise
        except Exception as error:
            logger.error("Summary worker failed: %s", type(error).__name__)
            if job:
                # A temporary DB failure may have prevented completion/failure persistence.
                # Keep ownership; retry persisting failure instead of orphaning running jobs.
                while True:
                    try:
                        await repository.fail_job(job, "内部存储故障，请查看日志")
                        break
                    except Exception as recovery_error:
                        logger.error("Job recovery failed: %s", type(recovery_error).__name__)
                        await asyncio.sleep(5)
            await asyncio.sleep(2)
