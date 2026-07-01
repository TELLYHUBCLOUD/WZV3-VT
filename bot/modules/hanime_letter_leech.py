from asyncio import Event, sleep
from html import escape
from re import sub

from pyrogram.errors import RPCError

from .. import LOGGER, task_dict, task_dict_lock
from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.hanime_poster import (
    build_hanime_caption,
    generate_hanime_poster,
)
from ..helper.ext_utils.hanime_state import hanime_bulk_run
from ..helper.ext_utils.site_resolvers import (
    discover_hanime_letter,
    hanime_url_from_slug,
    resolve_hanime,
)
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import send_message
from .batch_task_registry import BatchTaskController
from .ytdlp import YtDlp


def _parse_letter(message):
    parts = (message.text or "").split(maxsplit=1)
    value = parts[1].strip() if len(parts) > 1 else getattr(Config, "HANIME_LETTER", "")
    letter = str(value or "").strip()[:1].upper()
    if not letter or not letter.isalnum():
        return ""
    return letter


def _parse_chat(value, fallback_chat_id):
    if not value:
        return fallback_chat_id, None
    thread_id = None
    value = str(value).strip()
    if "|" in value:
        value, thread = value.split("|", 1)
        thread_id = int(thread) if thread.lstrip("-").isdigit() else None
    chat_id = int(value) if value.lstrip("-").isdigit() else value
    return chat_id, thread_id


def _clean_filename_part(value):
    value = sub(r"[\\/:*?\"<>|]+", " ", str(value or "")).strip()
    return sub(r"\s+", " ", value) or "Hanime Video"


def _hanime_base_name(metadata, quality):
    title = _clean_filename_part(metadata.get("title") or "Hanime Video")
    quality = _clean_filename_part(quality or "best")
    return f"🄰🅂- {title} [{quality}] ~ [@Anime_Starfall🥰]"


def _hanime_filename(metadata, quality):
    return f"{_hanime_base_name(metadata, quality)}.mkv"


async def _send_hanime_poster(message, metadata):
    caption = build_hanime_caption(metadata)
    if len(caption) > 1000:
        caption = f"{caption[:997].rstrip()}..."
    chat_id, thread_id = _parse_chat(getattr(Config, "HANIME_DUMP_CHAT", ""), message.chat.id)
    try:
        poster = await generate_hanime_poster(metadata)
        return await TgClient.bot.send_photo(
            chat_id=chat_id,
            photo=poster,
            caption=caption,
            message_thread_id=thread_id,
            disable_notification=True,
        )
    except Exception as e:
        LOGGER.warning(f"Hanime poster generation/send failed: {e}")
        try:
            return await TgClient.bot.send_message(
                chat_id=chat_id,
                text=caption,
                message_thread_id=thread_id,
                disable_web_page_preview=True,
                disable_notification=True,
            )
        except RPCError:
            return await send_message(message, caption)


async def _run_hanime_quality(client, message, controller, source_url, metadata, stream):
    quality = stream.get("label") or f"{stream.get('height')}p"
    base_name = _hanime_base_name(metadata, quality)
    filename = _hanime_filename(metadata, quality)
    opt = {
        "hanime_quality": str(stream.get("height") or quality).replace("p", ""),
        "merge_output_format": "mkv",
        "writethumbnail": False,
    }
    up_arg = ""
    if getattr(Config, "HANIME_DUMP_CHAT", ""):
        up_arg = f" -up {Config.HANIME_DUMP_CHAT}"
    task_msg = await send_message(
        message,
        (
            f"<b>Hanime Letter Leech</b>\n"
            f"<code>{escape(metadata.get('title') or 'Hanime')}</code>\n"
            f"Quality: <code>{escape(quality)}</code>"
        ),
    )
    task_msg = await client.get_messages(chat_id=task_msg.chat.id, message_ids=task_msg.id)
    task_msg.text = (
        f"/{BotCommands.YtdlLeechCommand[0]} {source_url}{up_arg} "
        f"-opt {opt!r} -n {base_name}"
    )
    if message.from_user:
        task_msg.from_user = message.from_user
    else:
        task_msg.sender_chat = message.sender_chat

    done_event = Event()
    download_event = Event()
    worker = YtDlp(
        client,
        task_msg,
        is_leech=True,
        hanime_letter_leech=True,
        hanime_metadata=metadata,
        hanime_quality=quality,
        hanime_output_name=filename,
        force_intro_subtitle=bool(getattr(Config, "HANIME_FORCE_INTRO_SUBTITLE", True)),
    )
    controller.register(worker)
    worker.bq_done_event = done_event
    worker.batch_download_event = download_event
    await worker.new_event()
    async with task_dict_lock:
        task_started = worker.mid in task_dict
    if not task_started and not done_event.is_set():
        done_event.set()
    await done_event.wait()
    return getattr(worker, "bq_result", "")


async def _run_hanime_streams(client, message, controller, source_url, metadata):
    for stream in metadata.get("streams") or []:
        if controller.cancelled:
            break
        result = await _run_hanime_quality(
            client, message, controller, source_url, metadata, stream
        )
        if result and result != "complete":
            LOGGER.warning(f"Hanime quality task ended with: {result}")
        await sleep(1)


@new_task
async def hanime_letter_leech(client, message):
    user_id = (message.from_user or message.sender_chat).id
    if user_id != int(Config.OWNER_ID):
        await send_message(message, "Only owner can use Hanime letter leech.")
        return

    letter = _parse_letter(message)
    if not letter:
        await send_message(message, f"Usage: <code>/{BotCommands.HanimeLetterLeechCommand[0]} A</code>")
        return

    try:
        async with hanime_bulk_run(user_id, letter):
            limit = int(getattr(Config, "HANIME_LETTER_MAX", 0) or 0)
            items = await discover_hanime_letter(letter, limit)
            if not items:
                await send_message(message, f"No Hanime titles found starting with <b>{escape(letter)}</b>.")
                return

            controller = BatchTaskController("hll", message)
            await send_message(
                message,
                (
                    f"<b>Hanime Letter Leech Started</b>\n"
                    f"Letter: <code>{escape(letter)}</code>\n"
                    f"Titles: <code>{len(items)}</code>\n"
                    f"Controller: <code>{controller.gid}</code>"
                ),
            )

            try:
                for index, item in enumerate(items, start=1):
                    if controller.cancelled:
                        break
                    source_url = item.get("source_url") or hanime_url_from_slug(item["slug"])
                    try:
                        metadata = await resolve_hanime(
                            source_url,
                            {
                                "HANIME_API_BASE": getattr(Config, "HANIME_API_BASE", "internal"),
                                "hanime_quality": "all",
                            },
                        )
                        metadata = {**item, **metadata}
                        await send_message(
                            message,
                            (
                                f"<b>Hanime {index}/{len(items)}</b>\n"
                                f"<code>{escape(metadata.get('title') or item.get('title') or source_url)}</code>"
                            ),
                        )
                        await _send_hanime_poster(message, metadata)
                        await _run_hanime_streams(client, message, controller, source_url, metadata)
                    except Exception as e:
                        LOGGER.error(f"Hanime letter item failed: {source_url}: {e}", exc_info=True)
                        await send_message(
                            message,
                            f"Hanime item failed, continuing:\n<code>{escape(str(e)[:900])}</code>",
                        )
                    delay = max(0, int(getattr(Config, "HANIME_LETTER_DELAY", 2) or 0))
                    if delay:
                        await sleep(delay)
            finally:
                controller.close()

            await send_message(message, "Hanime letter leech finished.")
    except RuntimeError as e:
        await send_message(message, str(e))
