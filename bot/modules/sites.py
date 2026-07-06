from html import escape
from json import loads as jloads
from re import split

from pyrogram.enums import ButtonStyle

from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import send_message


def _site_rows():
    raw = str(getattr(Config, "SITES_LINKS", "") or "").strip()
    if not raw:
        return []
    rows = []
    try:
        data = jloads(raw)
        if isinstance(data, dict):
            return [(str(k).strip(), str(v).strip()) for k, v in data.items() if str(v).strip()]
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("label") or item.get("title")
                    url = item.get("url") or item.get("link")
                    if name and url:
                        rows.append((str(name).strip(), str(url).strip()))
            if rows:
                return rows
    except Exception:
        pass
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = split(r"\s*(?:\||=>| - )\s*", line, maxsplit=1)
        if len(parts) == 2:
            name, url = parts
        else:
            url = line
            name = url.split("//", 1)[-1].split("/", 1)[0] or "Open"
        if url:
            rows.append((name.strip() or "Open", url.strip()))
    return rows


@new_task
async def sites(_, message):
    rows = _site_rows()
    if not rows:
        await send_message(
            message,
            "No site links configured. Owner can set <code>SITES_LINKS</code> in bot settings/config.",
        )
        return
    buttons = ButtonMaker()
    lines = ["<b>Useful Sites</b>"]
    for index, (name, url) in enumerate(rows, start=1):
        lines.append(f"{index}. {escape(name)}")
        buttons.url_button(escape(name), url, style=ButtonStyle.PRIMARY)
    await send_message(message, "\n".join(lines), buttons.build_menu(2))
