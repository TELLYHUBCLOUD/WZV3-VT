from asyncio import Lock
from contextlib import asynccontextmanager
from time import time

from ... import LOGGER
from ...core.config_manager import Config

ffmpeg_lock = Lock()


def _queue_enabled():
    return bool(getattr(Config, "FFMPEG_QUEUE_ENABLED", True))


def _queue_logs():
    return bool(getattr(Config, "FFMPEG_QUEUE_LOGS", True))


async def _notify(listener, text):
    if not listener or not _queue_logs():
        return
    try:
        from ..telegram_helper.message_utils import send_message

        await send_message(listener.message, text)
    except Exception as e:
        LOGGER.debug(f"FFmpeg queue notify skipped: {e}")


@asynccontextmanager
async def ffmpeg_task(listener=None, label="FFmpeg task"):
    if not _queue_enabled():
        yield
        return

    start_wait = time()
    if ffmpeg_lock.locked():
        LOGGER.info(f"Queue: Waiting for FFmpeg slot - {label}")
        await _notify(listener, f"Queue: Waiting for FFmpeg slot - <code>{label}</code>")

    async with ffmpeg_lock:
        waited = time() - start_wait
        if _queue_logs():
            if waited >= 1:
                LOGGER.info(f"FFmpeg task started - {label} after {waited:.1f}s wait")
            else:
                LOGGER.info(f"FFmpeg task started - {label}")
        try:
            yield
            if _queue_logs():
                LOGGER.info(f"FFmpeg task finished - {label}")
        except Exception as e:
            LOGGER.error(f"FFmpeg task failed - {label}: {e}")
            raise
