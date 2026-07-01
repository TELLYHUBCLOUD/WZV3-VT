from asyncio import Event, sleep
from html import escape
from os import path as ospath
from secrets import token_hex
from shutil import copy2

from aiofiles.os import makedirs, path as aiopath, remove
from aioshutil import rmtree
from aioqbt.api import AddFormBuilder
from natsort import natsorted

from .. import DOWNLOAD_DIR, LOGGER
from ..core.config_manager import Config
from ..core.torrent_manager import TorrentManager
from ..helper.ext_utils.bot_utils import new_task, sync_to_async
from ..helper.ext_utils.links_utils import is_magnet, is_url
from ..helper.ext_utils.status_utils import get_readable_file_size
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import edit_message, send_message
from .batch_task_registry import (
    BatchTaskController,
    finish_batch_plan,
    save_batch_plan,
    update_batch_plan,
)


def _batch_limit():
    try:
        gb = int(Config.BQLEECH_BATCH_SIZE_GB or 30)
    except (TypeError, ValueError):
        gb = 30
    return max(1, gb) * 1024**3


async def _copy_source_for_batch(source):
    if not await aiopath.exists(source):
        return source
    await makedirs(f"{DOWNLOAD_DIR}bqleech", exist_ok=True)
    dst = f"{DOWNLOAD_DIR}bqleech/{token_hex(4)}_{ospath.basename(source)}"
    await sync_to_async(copy2, source, dst)
    return dst


async def _resolve_source(message):
    text = message.text or message.caption or ""
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        value = parts[1].strip()
        if value and (is_magnet(value) or is_url(value) or await aiopath.exists(value)):
            return value, False

    reply = message.reply_to_message
    if reply and reply.document:
        filename = reply.document.file_name or ""
        if filename.endswith(".torrent") or reply.document.mime_type == "application/x-bittorrent":
            await makedirs(f"{DOWNLOAD_DIR}bqleech", exist_ok=True)
            path = await reply.download(file_name=f"{DOWNLOAD_DIR}bqleech/{token_hex(4)}_{filename}")
            return path, True
    return "", False


async def _add_metadata_torrent(source, tag):
    if not TorrentManager.qbittorrent:
        raise RuntimeError("qBittorrent is not initialized.")

    plan_dir = f"{DOWNLOAD_DIR}bqleech_plan_{tag}"
    await makedirs(plan_dir, exist_ok=True)
    form = AddFormBuilder.with_client(TorrentManager.qbittorrent)
    source_is_file = await aiopath.exists(source)
    if source_is_file:
        from aiofiles import open as aiopen

        async with aiopen(source, "rb") as f:
            form = form.include_file(await f.read())
    else:
        form = form.include_url(source)
    form = form.savepath(plan_dir).tags([tag]).stopped(source_is_file)
    await TorrentManager.qbittorrent.torrents.add(form.build())

    tor = None
    for _ in range(300):
        info = await TorrentManager.qbittorrent.torrents.info(tag=tag)
        if info:
            tor = info[0]
            if tor.state not in ("metaDL", "checkingResumeData") and tor.hash:
                files = await TorrentManager.qbittorrent.torrents.files(tor.hash)
                if files:
                    return tor, files, plan_dir
        await sleep(1)
    raise RuntimeError("qB metadata was not ready within 300 seconds.")


async def _cleanup_metadata_torrent(tor, tag, plan_dir):
    try:
        await TorrentManager.qbittorrent.torrents.delete([tor.hash], False)
    except Exception as e:
        LOGGER.warning(f"BQLeech metadata torrent cleanup failed: {e}")
    try:
        await TorrentManager.qbittorrent.torrents.delete_tags([tag])
    except Exception:
        pass
    if plan_dir and await aiopath.exists(plan_dir):
        await rmtree(plan_dir, ignore_errors=True)


def _plan_batches(files):
    limit = _batch_limit()
    batches = []
    skipped = []
    current = []
    current_size = 0
    ordered = sorted(files, key=lambda item: int(getattr(item, "index", 0)))

    for item in ordered:
        size = int(getattr(item, "size", 0) or 0)
        if size > limit:
            skipped.append(item)
            continue
        if current and current_size + size > limit:
            batches.append((current, current_size))
            current = []
            current_size = 0
        current.append(item)
        current_size += size
    if current:
        batches.append((current, current_size))
    return batches, skipped, limit


def _planner_text(tor_name, batches, skipped, limit):
    lines = [
        "<b>Big Queue Leech Planner</b>",
        f"Torrent: <code>{escape(tor_name)}</code>",
        f"Batch limit: <code>{get_readable_file_size(limit)}</code>",
        f"Batches: <code>{len(batches)}</code>",
        "",
    ]
    for index, (items, size) in enumerate(batches, start=1):
        first = items[0].name
        last = items[-1].name
        lines.append(
            f"{index}. <code>{get_readable_file_size(size)}</code> | "
            f"<code>{escape(first)}</code>"
            + (f" -> <code>{escape(last)}</code>" if last != first else "")
        )
    if skipped:
        lines.append("")
        lines.append("<b>Skipped oversized files:</b>")
        for item in skipped[:20]:
            lines.append(
                f"- <code>{escape(item.name)}</code> ({get_readable_file_size(item.size)})"
            )
        if len(skipped) > 20:
            lines.append(f"... and {len(skipped) - 20} more")
    return "\n".join(lines)


@new_task
async def bq_leech(client, message):
    source, remove_source = await _resolve_source(message)
    if not source:
        await send_message(
            message,
            f"Send <code>/{BotCommands.BigQLeechCommand[0]} magnet_or_torrent_link</code> or reply to a .torrent file.",
        )
        return

    status = await send_message(message, "Big Queue Leech: reading torrent metadata...")
    tag = f"bq_{message.id}_{token_hex(3)}"
    tor = None
    plan_dir = ""
    try:
        tor, files, plan_dir = await _add_metadata_torrent(source, tag)
        batches, skipped, limit = _plan_batches(files)
        controller = BatchTaskController("bqleech", message)
        await save_batch_plan(
            controller,
            {
                "source": str(source),
                "total_batches": len(batches),
                "batch_limit": limit,
                "torrent": tor.name,
            },
        )
        await edit_message(
            status,
            _planner_text(tor.name, batches, skipped, limit)
            + f"\n\nController GID: <code>{controller.gid}</code>\n"
            + f"Cancel: <code>/{BotCommands.CancelTaskCommand[0]}_{controller.gid}</code>",
        )
        await _cleanup_metadata_torrent(tor, tag, plan_dir)
        tor = None
        if not batches:
            await send_message(message, "Big Queue Leech: no files fit inside the batch limit.")
            return

        from .mirror_leech import Mirror

        all_ids = [str(item.index) for item in files]
        active = []
        next_batch = int(getattr(message, "bq_resume_index", 0) or 0)
        if next_batch:
            next_batch = min(max(next_batch, 0), len(batches))
            if next_batch >= len(batches):
                await send_message(message, "Big Queue Leech resume: all batches were already marked complete.")
                await finish_batch_plan(controller.gid)
                controller.close()
                return
            await update_batch_plan(controller.gid, current_index=next_batch)
            await send_message(
                message,
                (
                    "<b>Big Queue Leech resume</b>\n"
                    f"Starting from batch <code>{next_batch + 1}</code> of <code>{len(batches)}</code>."
                ),
            )

        while (next_batch < len(batches) or active) and not controller.cancelled:
            active = [item for item in active if not item["done"].is_set()]
            active_downloads = sum(1 for item in active if not item["download"].is_set())
            active_uploads = len(active)

            started = False
            while (
                next_batch < len(batches)
                and active_downloads < 1
                and active_uploads < 1
                and not controller.cancelled
            ):
                batch_no = next_batch + 1
                items, size = batches[next_batch]
                next_batch += 1
                await update_batch_plan(controller.gid, current_index=next_batch)
                started = True
                active_downloads += 1
                active_uploads += 1

                selected = [str(item.index) for item in items]
                unselected = [idx for idx in all_ids if idx not in selected]
                batch_source = await _copy_source_for_batch(source)
                cmd_text = f"/{BotCommands.QbLeechCommand[0]} {batch_source}"
                batch_msg = await send_message(
                    message,
                    (
                        f"<b>Big Queue Batch {batch_no}/{len(batches)}</b>\n"
                        f"Size: <code>{get_readable_file_size(size)}</code>\n"
                        f"Controller: <code>{controller.gid}</code>\n"
                        f"Starting qB leech task..."
                    ),
                )
                batch_msg = await client.get_messages(
                    chat_id=batch_msg.chat.id, message_ids=batch_msg.id
                )
                batch_msg.text = cmd_text
                if message.from_user:
                    batch_msg.from_user = message.from_user
                else:
                    batch_msg.sender_chat = message.sender_chat
                done_event = Event()
                download_event = Event()
                worker = Mirror(client, batch_msg, is_qbit=True, is_leech=True)
                controller.register(worker)
                worker.bq_selected_files = selected
                worker.bq_unselected_files = unselected
                worker.bq_batch_label = f"{batch_no}/{len(batches)}"
                worker.bq_remove_torrent_keep_files = True
                worker.bq_done_event = done_event
                worker.batch_download_event = download_event
                active.append(
                    {
                        "batch": batch_no,
                        "worker": worker,
                        "download": download_event,
                        "done": done_event,
                    }
                )
                await worker.new_event()
                if not getattr(worker, "bq_started", False) and not done_event.is_set():
                    done_event.set()
                    download_event.set()

            if controller.cancelled:
                break
            if not started:
                await sleep(2)
            else:
                await sleep(1)

        if controller.cancelled:
            await controller.cancel(controller.cancel_reason or "cancelled")
            await finish_batch_plan(controller.gid, cancelled=True)
            await send_message(
                message,
                f"Big Queue Leech stopped: <code>{escape(controller.cancel_reason or 'cancelled')}</code>",
            )
            controller.close()
            return
        await send_message(message, "Big Queue Leech: all planned batches finished.")
        await finish_batch_plan(controller.gid)
        controller.close()
    except Exception as e:
        LOGGER.error(f"BQLeech failed: {e}", exc_info=True)
        await edit_message(status, f"Big Queue Leech failed:\n<code>{escape(str(e))}</code>")
        if tor:
            await _cleanup_metadata_torrent(tor, tag, plan_dir)
    finally:
        if "controller" in locals():
            if controller.cancelled:
                await finish_batch_plan(controller.gid, cancelled=True)
            controller.close()
        if remove_source and source and await aiopath.exists(source):
            await remove(source)
