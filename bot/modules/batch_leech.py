from asyncio import Event, sleep
from html import escape

from httpx import AsyncClient

from .. import LOGGER, task_dict, task_dict_lock
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.links_utils import (
    is_gdrive_id,
    is_gdrive_link,
    is_magnet,
    is_mega_link,
    is_rclone_path,
    is_telegram_link,
    is_url,
)
from ..helper.ext_utils.status_utils import get_readable_file_size
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import send_message
from .batch_task_registry import (
    BatchTaskController,
    finish_batch_plan,
    get_batch_limits,
    save_batch_plan,
    update_batch_plan,
)


def _valid_link(value):
    return bool(
        value
        and (
            is_url(value)
            or is_magnet(value)
            or is_telegram_link(value)
            or is_gdrive_link(value)
            or is_gdrive_id(value)
            or is_mega_link(value)
            or is_rclone_path(value)
        )
    )


def _extract_links(message):
    text = message.text or message.caption or ""
    parts = text.split(maxsplit=1)
    raw = parts[1] if len(parts) > 1 else ""
    if not raw and message.reply_to_message:
        raw = message.reply_to_message.text or message.reply_to_message.caption or ""
    return [item.strip() for item in raw.replace("\n", " ").split() if _valid_link(item.strip())]


async def _http_size(link):
    if not link.startswith(("http://", "https://")):
        return 0
    try:
        async with AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.head(link)
            return int(resp.headers.get("content-length") or 0)
    except Exception:
        return 0


async def _filter_by_limit(links):
    try:
        limit_gb = float(getattr(Config, "BLEECH_LINK_SIZE_LIMIT_GB", 0) or 0)
    except (TypeError, ValueError):
        limit_gb = 0
    if limit_gb <= 0:
        return links, []
    limit = int(limit_gb * 1024**3)
    kept = []
    skipped = []
    for link in links:
        size = await _http_size(link)
        if size and size > limit:
            skipped.append((link, size))
        else:
            kept.append(link)
    return kept, skipped


@new_task
async def batch_leech(client, message):
    links = _extract_links(message)
    if not links:
        await send_message(
            message,
            f"Send <code>/{BotCommands.BatchLeechCommand[0]} link1 link2 link3</code> or reply to a text list.",
        )
        return

    links, skipped = await _filter_by_limit(links)
    if not links:
        await send_message(message, "Batch Leech: all links exceeded the per-link limit.")
        return

    controller = BatchTaskController("bleech", message)
    await save_batch_plan(controller, {"links": links, "total": len(links)})
    dl_limit, up_limit = get_batch_limits("bleech")
    plan = [
        "<b>Batch Leech Planner</b>",
        f"Controller GID: <code>{controller.gid}</code>",
        f"Links: <code>{len(links)}</code>",
        f"Active downloads/uploads: <code>{dl_limit}/{up_limit}</code>",
        f"Cancel: <code>/{BotCommands.CancelTaskCommand[0]}_{controller.gid}</code>",
    ]
    if skipped:
        plan.append("")
        plan.append("<b>Skipped over limit:</b>")
        for link, size in skipped[:10]:
            plan.append(f"- <code>{escape(link)}</code> ({get_readable_file_size(size)})")
    await send_message(message, "\n".join(plan))

    from .mirror_leech import Mirror

    active = []
    next_index = 0
    try:
        while (next_index < len(links) or active) and not controller.cancelled:
            active = [item for item in active if not item["done"].is_set()]
            active_downloads = sum(1 for item in active if not item["download"].is_set())
            active_uploads = len(active)
            started = False

            while (
                next_index < len(links)
                and active_downloads < dl_limit
                and active_uploads < up_limit
                and not controller.cancelled
            ):
                link = links[next_index]
                next_index += 1
                await update_batch_plan(controller.gid, current_index=next_index)
                active_downloads += 1
                active_uploads += 1
                started = True

                task_msg = await send_message(
                    message,
                    (
                        f"<b>Batch Leech {next_index}/{len(links)}</b>\n"
                        f"Controller: <code>{controller.gid}</code>\n"
                        f"Starting: <code>{escape(link[:180])}</code>"
                    ),
                )
                task_msg = await client.get_messages(
                    chat_id=task_msg.chat.id, message_ids=task_msg.id
                )
                task_msg.text = f"/{BotCommands.LeechCommand[0]} {link}"
                if message.from_user:
                    task_msg.from_user = message.from_user
                else:
                    task_msg.sender_chat = message.sender_chat

                done_event = Event()
                download_event = Event()
                worker = Mirror(client, task_msg, is_leech=True)
                controller.register(worker)
                worker.bq_done_event = done_event
                worker.batch_download_event = download_event
                active.append(
                    {
                        "worker": worker,
                        "download": download_event,
                        "done": done_event,
                    }
                )
                await worker.new_event()
                async with task_dict_lock:
                    task_started = worker.mid in task_dict
                if not task_started and not done_event.is_set():
                    download_event.set()
                    done_event.set()

            if controller.cancelled:
                break
            await sleep(1 if started else 2)

        if controller.cancelled:
            await controller.cancel(controller.cancel_reason or "cancelled")
            await send_message(
                message,
                f"Batch Leech stopped: <code>{escape(controller.cancel_reason or 'cancelled')}</code>",
            )
            return
        await send_message(message, "Batch Leech: all planned links finished.")
    except Exception as e:
        LOGGER.error(f"Batch Leech failed: {e}", exc_info=True)
        await send_message(message, f"Batch Leech failed:\n<code>{escape(str(e))}</code>")
    finally:
        await finish_batch_plan(controller.gid, cancelled=controller.cancelled)
        controller.close()
