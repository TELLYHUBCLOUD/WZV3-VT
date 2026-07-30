from json import dumps, loads
from os import makedirs, path as ospath
from secrets import token_hex

from .. import DOWNLOAD_DIR, LOGGER
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.db_handler import database
from ..helper.ext_utils.tamilmv_resolver import download_torrent, resolve_tamilmv
from ..helper.telegram_helper.message_utils import edit_message, send_message
from .rss import _parse_chat_value, _start_rss_download


def _destination(message, raw):
    value = str(raw or "").strip()
    if value:
        return value
    thread = getattr(message, "message_thread_id", None)
    return f"{message.chat.id}|{thread}" if thread else str(message.chat.id)


def _seen_items():
    try:
        data = loads(str(getattr(Config, "TMV_SEEN_ITEMS", "") or "[]"))
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()


async def _save_seen(seen):
    value = dumps(sorted(seen)[-2000:], separators=(",", ":"))
    Config.set("TMV_SEEN_ITEMS", value)
    await database.update_config({"TMV_SEEN_ITEMS": value})


async def queue_tamilmv_candidates(candidates, destination, owner_id, seen=None):
    seen = seen if seen is not None else _seen_items()
    chat_id, topic_id = _parse_chat_value(destination)
    if not chat_id:
        raise ValueError("Invalid TamilMV upload destination")
    workdir = ospath.join(DOWNLOAD_DIR, ".tmv")
    makedirs(workdir, exist_ok=True)
    queued = 0
    category = str(getattr(Config, "TMV_CATEGORY", "") or "").strip().lower()
    for item in candidates:
        if (
            category
            and category not in {"all", "tamil", item.category}
            and category not in item.title.lower()
        ):
            continue
        url_key = f"url:{item.torrent_url}"
        if url_key in seen:
            continue
        target = ospath.join(workdir, f"{token_hex(8)}.torrent")
        try:
            infohash = await download_torrent(item.torrent_url, target)
        except Exception as error:
            LOGGER.warning("TMV torrent skipped (%s): %s", item.torrent_url, error)
            continue
        hash_key = f"hash:{infohash}"
        if hash_key in seen:
            try:
                from aiofiles.os import remove

                await remove(target)
            except Exception:
                pass
            continue
        await _start_rss_download(
            url=target,
            command="qbleech",
            user_id=owner_id,
            rss_chat_id=chat_id,
            rss_topic_id=topic_id,
            item_title=item.title,
            auto_leech=True,
            rename_mode="title",
            leech_by="bot",
            rss_upload_chat=destination,
        )
        seen.update((url_key, hash_key))
        queued += 1
    if queued:
        await _save_seen(seen)
    return queued


@new_task
async def tamilmv(_, message):
    parts = (message.text or "").split()
    if len(parts) < 2:
        await send_message(message, "Usage: <code>/tmv site-or-topic-url [-up CHAT_ID|TOPIC_ID]</code>")
        return
    source = parts[1]
    upload = ""
    if "-up" in parts:
        index = parts.index("-up")
        if index + 1 < len(parts):
            upload = parts[index + 1]
    notice = await send_message(message, "🔎 <b>TamilMV:</b> resolving torrent links...")
    try:
        candidates = await resolve_tamilmv(source)
        if not candidates:
            await edit_message(notice, "No downloadable TamilMV torrents were found.")
            return
        queued = await queue_tamilmv_candidates(
            candidates,
            _destination(message, upload),
            message.from_user.id,
        )
        await edit_message(notice, f"✅ TamilMV queued <b>{queued}</b> new torrent(s).")
    except Exception as error:
        LOGGER.error("TamilMV command failed: %s", error, exc_info=True)
        await edit_message(
            notice, f"TamilMV resolver failed: <code>{str(error)[:800]}</code>"
        )
