from asyncio import sleep

from pyrogram.errors import FloodWait

from .. import LOGGER
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.message_utils import send_message


def _parse_clonechannel(text):
    parts = (text or "").split()
    if len(parts) < 4 or "-up" not in parts:
        return "", ""
    up_index = parts.index("-up")
    source = parts[1] if up_index > 1 else ""
    dest = parts[up_index + 1] if up_index + 1 < len(parts) else ""
    return source, dest


async def _resolve_source(client, source):
    try:
        return await client.join_chat(source)
    except Exception as e:
        if "USER_ALREADY_PARTICIPANT" in str(e) or "already" in str(e).lower():
            return await client.get_chat(source)
        LOGGER.info(f"clonechannel: join skipped/failed for {source}: {e}")
    return await client.get_chat(source)


async def _forward_one(client, dest, source_id, message_id):
    while True:
        try:
            await client.forward_messages(
                chat_id=dest,
                from_chat_id=source_id,
                message_ids=message_id,
                disable_notification=True,
            )
            return True
        except FloodWait as e:
            await sleep(int(getattr(e, "value", 0) or 0) + 2)
        except Exception as e:
            LOGGER.warning(f"clonechannel: failed to forward {message_id}: {e}")
            return False


@new_task
async def clone_channel(_, message):
    source, dest = _parse_clonechannel(message.text)
    if not source or not dest:
        await send_message(
            message,
            "<b>Usage:</b> <code>/clonechannel channel_join_link -up -100...</code>",
        )
        return

    client = TgClient.user if TgClient.user else TgClient.bot
    status = await send_message(message, "<b>Clone channel started...</b>")
    try:
        source_chat = await _resolve_source(client, source)
    except Exception as e:
        await send_message(message, f"<b>Clone channel failed:</b> <code>{e}</code>")
        return

    forwarded = failed = 0
    message_ids = []
    async for item in client.get_chat_history(source_chat.id):
        message_ids.append(item.id)

    for message_id in reversed(message_ids):
        if await _forward_one(client, dest, source_chat.id, message_id):
            forwarded += 1
        else:
            failed += 1
        if (forwarded + failed) % 50 == 0:
            try:
                if not isinstance(status, str):
                    await status.edit(
                        f"<b>Clone channel running...</b>\n"
                        f"Forwarded: <code>{forwarded}</code>\n"
                        f"Failed: <code>{failed}</code>"
                    )
            except Exception:
                pass

    await send_message(
        message,
        f"<b>Clone channel done.</b>\n"
        f"Forwarded: <code>{forwarded}</code>\n"
        f"Failed: <code>{failed}</code>",
    )
