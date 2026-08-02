from html import escape

from .. import LOGGER, user_data
from ..helper.ext_utils.bot_utils import new_task
from ..helper.poster_engine import (
    POSTER_TEMPLATE_COUNT,
    render_poster_option,
    search_poster_metadata,
)
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)

POSTER_SEARCH_CACHE = {}


def _query_from_message(message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        return parts[1].strip()
    reply = getattr(message, "reply_to_message", None)
    if reply:
        if getattr(reply, "text", None):
            return reply.text.strip()
        for media in ("video", "document", "audio"):
            item = getattr(reply, media, None)
            if item and getattr(item, "file_name", None):
                return item.file_name
    return ""


def _summary(metadata):
    lines = [
        "<b>Poster Search</b>",
        "",
        f"<b>Title:</b> {escape(str(metadata.get('title') or metadata.get('name') or 'Unknown'))}",
        f"<b>Type:</b> {escape(str(metadata.get('category') or 'movie'))}",
    ]
    if metadata.get("year"):
        lines.append(f"<b>Year:</b> <code>{escape(str(metadata.get('year')))}</code>")
    if metadata.get("genres"):
        lines.append(f"<b>Genres:</b> {escape(str(metadata.get('genres')))}")
    if metadata.get("provider"):
        lines.append(f"<b>Provider:</b> {escape(str(metadata.get('provider')))}")
    lines.append("")
    lines.append("Choose a poster template to save as your thumbnail.")
    return "\n".join(lines)


@new_task
async def poster_search(_, message):
    query = _query_from_message(message)
    if not query:
        await send_message(message, "Send a title with /poster or reply to a file/message.")
        return
    wait = await send_message(message, f"Searching poster for <code>{escape(query)}</code>...")
    user_dict = user_data.get(message.from_user.id, {})
    try:
        metadata = await search_poster_metadata(query, user_dict)
        if not metadata:
            await edit_message(wait, "No metadata found.")
            return
        POSTER_SEARCH_CACHE[(message.from_user.id, wait.id)] = metadata
        buttons = ButtonMaker()
        for i in range(1, POSTER_TEMPLATE_COUNT + 1):
            buttons.data_button(f"Style {i}", f"psel {message.from_user.id} {wait.id} {i}")
        buttons.data_button("Close", f"psel {message.from_user.id} {wait.id} close", "footer")
        preview = await render_poster_option(metadata, message.from_user.id, user_dict, "1")
        await delete_message(wait)
        sent = await send_message(message, _summary(metadata), buttons.build_menu(3), photo=preview)
        POSTER_SEARCH_CACHE[(message.from_user.id, sent.id)] = metadata
    except Exception as err:
        LOGGER.error(f"Poster search failed: {err}", exc_info=True)
        await edit_message(wait, f"Poster search failed: <code>{escape(str(err)[:500])}</code>")


@new_task
async def poster_select(_, query):
    data = query.data.split()
    user_id = query.from_user.id
    if len(data) < 4 or int(data[1]) != user_id:
        await query.answer("Not yours.", show_alert=True)
        return
    msg_id = int(data[2])
    action = data[3]
    metadata = POSTER_SEARCH_CACHE.get((user_id, msg_id)) or POSTER_SEARCH_CACHE.get((user_id, query.message.id))
    if action == "close":
        await query.answer()
        await delete_message(query.message)
        return
    if not metadata:
        await query.answer("Search expired. Run /poster again.", show_alert=True)
        return
    user_dict = user_data.get(user_id, {})
    try:
        path = await render_poster_option(metadata, user_id, user_dict, action, save_thumbnail=True)
        await query.answer(f"Saved style {action}", show_alert=True)
        await edit_message(
            query.message,
            f"<b>Poster style {escape(action)} saved as thumbnail.</b>\n<code>{escape(path)}</code>",
        )
    except Exception as err:
        LOGGER.error(f"Poster select failed: {err}", exc_info=True)
        await query.answer("Failed to save poster.", show_alert=True)
