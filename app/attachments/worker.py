import asyncio
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import httpx

from app.attachments.storage import AttachmentStorage
from app.config import AttachmentConfig
from app.onebot.files import FileResolver, validate_download_target
from app.storage.inbox_repository import InboxRepository
from app.storage.policy_repository import PolicyRepository

logger = logging.getLogger(__name__)


class SizeLimitError(Exception):
    pass


class IncompleteDownloadError(Exception):
    pass


class AttachmentWorker:
    def __init__(self, repository: InboxRepository, resolver: FileResolver, storage: AttachmentStorage,
                 config: AttachmentConfig, allowed_groups: list[int], client: httpx.AsyncClient,
                 check_target: Callable[[str], Awaitable[None]] | None = None) -> None:
        self.repository = repository
        self.resolver = resolver
        self.storage = storage
        self.config = config
        self.allowed_groups = frozenset(allowed_groups)
        self.client = client
        self.check_target = check_target or validate_download_target

    async def download(self, attachment: dict, url: str) -> tuple[str, str, int]:
        limit = self.config.max_auto_download_mb * 1024**2
        temporary, final = await asyncio.to_thread(self.storage.prepare, attachment)
        try:
            async with asyncio.timeout(300):
                for redirects in range(4):
                    await self.check_target(url)
                    async with self.client.stream("GET", url, headers={"Accept-Encoding": "identity"},
                                                  follow_redirects=False) as response:
                        if response.is_redirect:
                            if redirects == 3:
                                raise ValueError("Too many download redirects")
                            url = urljoin(url, response.headers.get("location", ""))
                            continue
                        response.raise_for_status()
                        length = response.headers.get("content-length")
                        if length is not None and int(length) > limit:
                            raise SizeLimitError()
                        digest, total = hashlib.sha256(), 0
                        # Exclusive creation prevents following an existing partial-file link.
                        with temporary.open("xb") as output:
                            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                                total += len(chunk)
                                if total > limit:
                                    raise SizeLimitError()
                                await asyncio.to_thread(output.write, chunk)
                                digest.update(chunk)
                            await asyncio.to_thread(output.flush)
                            await asyncio.to_thread(os.fsync, output.fileno())
                        expected = attachment["file_size"]
                        if ((expected is not None and total != expected)
                                or (length is not None and total != int(length))):
                            raise IncompleteDownloadError()
                        await asyncio.to_thread(self.storage.finish, temporary, final)
                        return str(final), digest.hexdigest(), total
                raise ValueError("Download did not return content")
        finally:
            if temporary.exists():
                # A partial file never receives downloaded status or its final name.
                await asyncio.to_thread(temporary.unlink)

    async def process(self, attachment: dict) -> None:
        logger.info("Attachment downloading id=%s attempt=%s", attachment["id"], attachment["attempts"])
        try:
            if not self.config.auto_download or attachment["group_id"] not in self.allowed_groups:
                await self.repository.download_result(attachment, "skipped", "policy_disabled")
                self.resolver.forget(attachment)
                logger.info("Attachment skipped id=%s reason=policy_disabled", attachment["id"])
                return
            policy = await PolicyRepository(self.repository.db, list(self.allowed_groups)).get(attachment["self_id"], attachment["group_id"])
            if policy.mode == "ignore" or not policy.attachment_download_enabled:
                await self.repository.download_result(attachment, "skipped", "group_policy")
                self.resolver.forget(attachment)
                logger.info("Attachment skipped id=%s reason=group_policy", attachment["id"])
                return
            if attachment["attempts"] > self.config.retry_count + 1:
                raise ValueError("Retry budget exhausted after interruption")
            if (attachment["file_size"] is not None and
                    attachment["file_size"] > self.config.max_auto_download_mb * 1024**2):
                raise SizeLimitError()
            url = await self.resolver.resolve(attachment)
            path, digest, size = await self.download(attachment, url)
            await self.repository.download_result(attachment, "downloaded", path=path, sha256=digest, size=size)
            self.resolver.forget(attachment)
            logger.info("Attachment downloaded id=%s bytes=%s", attachment["id"], size)
        except SizeLimitError:
            await self.repository.download_result(attachment, "skipped", "size_limit")
            self.resolver.forget(attachment)
            logger.info("Attachment skipped id=%s reason=size_limit", attachment["id"])
        except Exception as error:
            retryable = isinstance(error, (httpx.TransportError, ConnectionError, TimeoutError, IncompleteDownloadError))
            if isinstance(error, httpx.HTTPStatusError):
                retryable = error.response.status_code in {408, 429} or error.response.status_code >= 500
            status = "pending" if retryable and attachment["attempts"] <= self.config.retry_count else "failed"
            await self.repository.download_result(attachment, status, type(error).__name__)
            if status == "failed":
                self.resolver.forget(attachment)
            logger.warning("Attachment %s id=%s error=%s", status, attachment["id"], type(error).__name__)

    async def run(self) -> None:
        while True:
            try:
                actions = self.resolver.actions
                if not self.config.enabled or not actions.connected or actions.self_id is None:
                    await asyncio.sleep(1)
                    continue
                attachment = await self.repository.claim_attachment(actions.self_id)
                if attachment:
                    await self.process(attachment)
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info("Attachment worker stopped; interrupted downloads resume after restart")
                raise
            except Exception as error:
                logger.error("Attachment worker failure: %s", type(error).__name__)
                await asyncio.sleep(2)
