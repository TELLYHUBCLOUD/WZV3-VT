from html import escape
from os import path as ospath
from re import sub as re_sub
from shlex import split as shlex_split
from shutil import copy2
from time import time
from urllib.parse import unquote, urlsplit

from aiofiles import open as aiopen
from aiofiles.os import listdir, makedirs, path as aiopath
from aioshutil import rmtree
from httpx import AsyncClient
from PIL import Image, ImageDraw, ImageFont, ImageOps

from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import cmd_exec, new_task, sync_to_async
from ..helper.ext_utils.media_utils import (
    format_clean_poster_title,
    get_release_description,
    get_media_info,
    get_multiple_frames_thumbnail,
    get_video_thumbnail,
    take_ss,
)
from ..helper.ext_utils.status_utils import get_readable_file_size
from ..helper.telegram_helper.message_utils import edit_message, send_file, send_message

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".ts", ".m2ts", ".mpg", ".mpeg", ".vob", ".m4v", ".3gp",
    ".ogv", ".divx", ".rmvb", ".asf",
}


def _safe_name(name, fallback="release.mkv"):
    name = unquote((name or "").split("?", 1)[0].rsplit("/", 1)[-1]).strip()
    if not name:
        name = fallback
    name = re_sub(r'[\\/:*?"<>|\r\n]+', "_", name)
    name = re_sub(r"\s+", " ", name).strip(" .")
    return name[:180] or fallback


def _tracker_list():
    raw = str(getattr(Config, "CTORRENT_TRACKERS", "") or "")
    trackers = []
    for part in re_sub(r"[,\r]+", "\n", raw).splitlines():
        tracker = part.strip()
        if tracker and tracker not in trackers:
            trackers.append(tracker)
    return trackers


async def _unique_path(directory, filename):
    await makedirs(directory, exist_ok=True)
    stem, ext = ospath.splitext(filename)
    candidate = ospath.join(directory, filename)
    count = 2
    while await aiopath.exists(candidate):
        candidate = ospath.join(directory, f"{stem}_{count}{ext}")
        count += 1
    return candidate


async def _edit_progress(message, title, current=0, total=0, extra=""):
    if total:
        percent = round(current * 100 / total, 2)
        size = f"{get_readable_file_size(current)} / {get_readable_file_size(total)}"
        text = f"{title}\nProgress: {percent}%\nDone: {size}"
    elif current:
        text = f"{title}\nDone: {get_readable_file_size(current)}"
    else:
        text = title
    if extra:
        text += f"\n{extra}"
    return await edit_message(message, text)


async def _download_link(link, out_dir, status_msg):
    filename = _safe_name(urlsplit(link).path, "release.mkv")
    out_path = await _unique_path(out_dir, filename)
    downloaded = 0
    total = 0
    last_edit = 0

    async with AsyncClient(follow_redirects=True, timeout=None) as client:
        async with client.stream("GET", link) as response:
            response.raise_for_status()
            if cd := response.headers.get("content-disposition"):
                if "filename=" in cd:
                    filename = _safe_name(cd.rsplit("filename=", 1)[-1].strip('" '))
                    out_path = await _unique_path(out_dir, filename)
            total = int(response.headers.get("content-length") or 0)
            async with aiopen(out_path, "wb") as f:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    await f.write(chunk)
                    now = time()
                    if now - last_edit > 5:
                        await _edit_progress(
                            status_msg,
                            "Create Torrent: downloading source...",
                            downloaded,
                            total,
                        )
                        last_edit = now
    return out_path


async def _download_reply(message, out_dir, status_msg):
    reply = message.reply_to_message
    media = getattr(reply, reply.media.value) if reply and reply.media else None
    if not media:
        return None
    filename = _safe_name(getattr(media, "file_name", "") or "telegram_video.mkv")
    out_path = await _unique_path(out_dir, filename)
    total = getattr(media, "file_size", 0) or 0
    last_edit = 0

    async def progress(current, _):
        nonlocal last_edit
        now = time()
        if now - last_edit > 5:
            await _edit_progress(
                status_msg,
                "Create Torrent: downloading Telegram file...",
                current,
                total,
            )
            last_edit = now

    downloaded = await reply.download(file_name=out_path, progress=progress)
    return downloaded or out_path


def _safe_local_path(value):
    allowed_roots = (
        "/usr/src/app/downloads/",
        "/usr/src/app/torrents/",
    )
    if not value.startswith(allowed_roots):
        return ""
    return value


async def _resolve_source(message, status_msg):
    storage_dir = str(Config.CTORRENT_STORAGE_DIR or "/usr/src/app/torrents/seeding")
    await makedirs(storage_dir, exist_ok=True)
    text = message.text or message.caption or ""
    args = text.split(maxsplit=1)
    value = args[1].strip() if len(args) > 1 else ""
    if value.startswith(("http://", "https://")):
        return await _download_link(value, storage_dir, status_msg)
    if value and (local_path := _safe_local_path(value)) and await aiopath.exists(local_path):
        if local_path.startswith(storage_dir.rstrip("/") + "/"):
            return local_path
        dest = await _unique_path(storage_dir, _safe_name(ospath.basename(local_path)))
        await sync_to_async(copy2, local_path, dest)
        return dest
    if message.reply_to_message:
        return await _download_reply(message, storage_dir, status_msg)
    return None


def _parse_folder_mode(message):
    text = message.text or message.caption or ""
    args = text.split(maxsplit=1)
    if len(args) < 2 or " - " not in args[1]:
        return None
    try:
        tokens = shlex_split(args[1])
    except ValueError:
        return None
    if "-" not in tokens:
        return None
    sep = tokens.index("-")
    if sep == 0:
        return None
    folder_name = _safe_name(" ".join(tokens[:sep]), "release_folder")
    links = [
        token for token in tokens[sep + 1 :]
        if token.startswith(("http://", "https://"))
    ]
    return folder_name, links


async def _make_torrent(source_path):
    output_dir = str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output")
    await makedirs(output_dir, exist_ok=True)
    torrent_name = f"{ospath.basename(source_path).strip()}.torrent"
    torrent_path = await _unique_path(output_dir, _safe_name(torrent_name, "release.torrent"))
    cmd = ["mktorrent", "-o", torrent_path]
    if Config.CTORRENT_PRIVATE:
        cmd.append("-p")
    trackers = _tracker_list()
    if trackers:
        cmd.extend(["-a", ",".join(trackers)])
    cmd.append(source_path)
    stdout, stderr, code = await cmd_exec(cmd)
    if code != 0 or not await aiopath.exists(torrent_path):
        raise RuntimeError(stderr or stdout or "mktorrent failed")
    return torrent_path, trackers


def _font(size):
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _truncate(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else f"{text[: max(0, limit - 3)]}..."


def _format_duration(seconds):
    seconds = max(0, int(float(seconds or 0)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}"


def _normalize_channels(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return str(value or "")
    return {
        1: "1.0",
        2: "2.0",
        6: "5.1",
        8: "7.1",
    }.get(value, f"{value}ch")


def _clean_codec(value):
    value = str(value or "").lower()
    if value in {"hevc", "h265", "x265"}:
        return "H.265/HEVC"
    if value in {"h264", "avc", "x264"}:
        return "H.264/AVC"
    if value == "eac3":
        return "E-AC3"
    return value.upper() if value else ""


async def _probe_summary(source_path):
    summary = {
        "title": ospath.basename(source_path),
        "size": get_readable_file_size(await aiopath.getsize(source_path)),
        "duration": "",
        "resolution": "",
        "video": "",
        "bit_depth": "",
        "dynamic_range": "",
        "audio": "",
        "subtitles": "",
        "source_folder": ospath.dirname(source_path),
    }
    try:
        stdout, _, code = await cmd_exec(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                source_path,
            ]
        )
        if code != 0 or not stdout:
            return summary
        import json

        data = json.loads(stdout)
        fmt = data.get("format") or {}
        summary["duration"] = _format_duration(fmt.get("duration") or 0)
        audio = []
        subs = []
        for stream in data.get("streams", []):
            codec_type = stream.get("codec_type")
            tags = stream.get("tags") or {}
            lang = tags.get("language") or "und"
            if codec_type == "video" and not summary["video"]:
                width = stream.get("width")
                height = stream.get("height")
                if width and height:
                    summary["resolution"] = f"{width}x{height}"
                summary["video"] = _clean_codec(stream.get("codec_name"))
                bit_depth = (
                    stream.get("bits_per_raw_sample")
                    or stream.get("bits_per_sample")
                    or ""
                )
                if bit_depth:
                    summary["bit_depth"] = f"{bit_depth}bit"
                dyn_src = " ".join(
                    str(stream.get(k, ""))
                    for k in ("color_transfer", "color_primaries", "pix_fmt", "profile")
                ).lower()
                if "dovi" in dyn_src or "dolby" in dyn_src:
                    summary["dynamic_range"] = "DV"
                elif "smpte2084" in dyn_src or "hdr" in dyn_src:
                    summary["dynamic_range"] = "HDR"
                else:
                    summary["dynamic_range"] = "SDR"
            elif codec_type == "audio":
                item = " ".join(
                    part
                    for part in (
                        lang.upper(),
                        _clean_codec(stream.get("codec_name")),
                        _normalize_channels(stream.get("channels")),
                    )
                    if part
                )
                if item and item not in audio:
                    audio.append(item)
            elif codec_type == "subtitle":
                if lang and lang not in subs:
                    subs.append(lang)
        summary["audio"] = ", ".join(audio[:4])
        summary["subtitles"] = ", ".join(subs[:8])
    except Exception:
        return summary
    return summary


def _compact_media_info(summary):
    return "\n".join(
        f"{label}: {value}"
        for label, value in (
            ("File Size", summary.get("size")),
            ("Duration", summary.get("duration")),
            ("Resolution", summary.get("resolution")),
            ("Video", " ".join(
                part
                for part in (
                    summary.get("video"),
                    summary.get("bit_depth"),
                    summary.get("dynamic_range"),
                )
                if part
            )),
            ("Audio", summary.get("audio")),
            ("Subtitles", summary.get("subtitles")),
        )
        if value
    )


async def _create_contact_sheet_with_header(source_path, summary, title, output_dir=None):
    shots_dir = await take_ss(source_path, 15)
    if not shots_dir:
        return await get_multiple_frames_thumbnail(source_path, "5x3", False)
    try:
        shots = [
            ospath.join(shots_dir, name)
            for name in sorted(await listdir(shots_dir))
            if name.lower().endswith((".jpg", ".jpeg", ".png"))
        ][:15]
        if len(shots) < 15:
            return await get_multiple_frames_thumbnail(source_path, "5x3", False)

        output_dir = output_dir or str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output")
        await makedirs(output_dir, exist_ok=True)
        output = await _unique_path(output_dir, f"{_safe_name(title, 'contact_sheet')}.jpg")
        cols, rows = 5, 3
        cell_w, cell_h = 320, 180
        gap, outer, header_h, caption_h = 12, 18, 132, 24
        width = outer * 2 + cols * cell_w + (cols - 1) * gap
        height = outer * 2 + header_h + rows * (cell_h + caption_h) + (rows - 1) * gap
        canvas = Image.new("RGB", (width, height), "#15171c")
        draw = ImageDraw.Draw(canvas)
        title_font = _font(24)
        body_font = _font(15)
        small_font = _font(13)

        draw.text((outer, outer), _truncate(title, 82), fill="#ffffff", font=title_font)
        header_lines = [
            f"File: {_truncate(ospath.basename(source_path), 96)}",
            " | ".join(
                part
                for part in (
                    f"Size {summary.get('size')}",
                    f"Duration {summary.get('duration')}",
                    f"Resolution {summary.get('resolution')}",
                    f"Video {summary.get('video')} {summary.get('bit_depth')} {summary.get('dynamic_range')}".strip(),
                )
                if part and not part.endswith(" ")
            ),
            f"Audio: {_truncate(summary.get('audio') or 'Unknown', 120)}",
        ]
        for index, line in enumerate(header_lines):
            draw.text((outer, outer + 34 + index * 22), line, fill="#d5d8df", font=body_font)

        duration = (await get_media_info(source_path))[0] or 0
        interval = duration / 16 if duration else 0
        for index, shot in enumerate(shots):
            row = index // cols
            col = index % cols
            left = outer + col * (cell_w + gap)
            top = outer + header_h + row * (cell_h + caption_h + gap)
            with Image.open(shot) as image:
                image = image.convert("RGB")
                image = ImageOps.contain(image, (cell_w, cell_h), Image.Resampling.LANCZOS)
                frame = Image.new("RGB", (cell_w, cell_h), "#0d0f14")
                frame.paste(image, ((cell_w - image.width) // 2, (cell_h - image.height) // 2))
                canvas.paste(frame, (left, top))
            draw.rectangle([left, top, left + cell_w - 1, top + cell_h - 1], outline="#343844", width=1)
            draw.text(
                (left + 8, top + cell_h + 5),
                f"{index + 1:02}  {_format_duration(interval * (index + 1))}",
                fill="#cdd1d8",
                font=small_font,
            )
        canvas.save(output, "JPEG", quality=95, optimize=True)
        return output
    finally:
        await rmtree(shots_dir, ignore_errors=True)


BBCODE_TEMPLATES = {
    "anime_release": """[BG=https://images2.imgbox.com/88/eb/JAaZN8L1_o.jpg,100%][font=Palatino Linotype][color=#FFFFFF][BR]
[BG=#343434C0,60%,center][BG=#34343400,96%,center][BR][size=8][align=center][i] {title} [/i][/align][/size][BR][/BG][/BG]

[BG=#343434C0,85%,center][BG=#34343400,98%,center][align=center]
###

[BR][/align][/BG][/BG]

[BG=#343434C0,85%,center][BG=#34343400,96%,center][align=center][img]https://images2.imgbox.com/94/6c/om852SEy_o.png[/img][/align]
[size=2][align=justify][i]
{description}

{media_info}

[/i][/align][/size]
[/BG][/BG]

[BG=#343434C0,85%,center][BG=#34343400,98%,center][align=center][img]https://images2.imgbox.com/62/cd/IpX6mU4M_o.png[/img]
[BR]###
[BR][/align][/BG][/BG]
[/COLOR][/FONT][/BG]""",
    "dark_bg": """[center][size=6][b]{title}[/b][/size][/center]

[center][img]{thumbnail_link}[/img][/center]

[b]Description[/b]
{description}

[b]Media Info[/b]
[code]{media_info}[/code]

[center][img]{contact_sheet_link}[/img][/center]""",
    "classic": """[center][b][size=5]{title}[/size][/b][/center]

[b]Description[/b]
{description}

[b]Media Info[/b]
[spoiler]{media_info}[/spoiler]

Thumbnail: {thumbnail_link}
Contact Sheet: {contact_sheet_link}
Screenshots: {screenshots_link}""",
    "minimal": """[b]{title}[/b]

{description}

{media_info}

Thumbnail: {thumbnail_link}
Contact Sheet: {contact_sheet_link}""",
}


async def _load_bbcode_template():
    template_path = str(getattr(Config, "CTORRENT_BBCODE_TEMPLATE_PATH", "") or "").strip()
    if template_path and await aiopath.exists(template_path):
        async with aiopen(template_path, encoding="utf-8", errors="ignore") as f:
            return await f.read()
    preset = str(getattr(Config, "CTORRENT_BBCODE_TEMPLATE", "anime_release") or "anime_release").strip().lower()
    return BBCODE_TEMPLATES.get(preset) or BBCODE_TEMPLATES["anime_release"]


async def _write_description(source_path, title, summary):
    output_dir = str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output")
    media_info = _compact_media_info(summary)
    description = await get_release_description(title)
    template = await _load_bbcode_template()
    values = {
        "title": title,
        "description": description,
        "media_info": media_info,
        "mediainfo": media_info,
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value or ""))
    desc_path = await _unique_path(output_dir, f"{_safe_name(title, 'description')}.description.txt")
    async with aiopen(desc_path, "w", encoding="utf-8") as f:
        await f.write(rendered.rstrip() + "\n")
    return desc_path


async def _send_artifacts(message, source_path, torrent_path, trackers):
    size = await aiopath.getsize(source_path)
    title, _, _ = format_clean_poster_title(ospath.basename(source_path))
    title = title or ospath.splitext(ospath.basename(source_path))[0]
    summary = await _probe_summary(source_path)
    thumb = await get_video_thumbnail(source_path, None)
    sheet = await _create_contact_sheet_with_header(source_path, summary, title)
    desc_path = await _write_description(source_path, title, summary)

    if thumb:
        await send_file(message, thumb, "HD video thumbnail")
    else:
        await send_message(message, "Create Torrent: HD thumbnail failed, continuing.")

    if sheet:
        await send_file(message, sheet, "15 screenshot contact sheet")
    else:
        await send_message(message, "Create Torrent: contact sheet failed, continuing.")

    await send_file(message, desc_path, "BBCode description")

    caption = (
        "<b>Created Torrent</b>\n"
        f"File: <code>{escape(ospath.basename(source_path))}</code>\n"
        f"Size: <code>{get_readable_file_size(size)}</code>\n"
        f"Title: <code>{escape(title)}</code>\n"
        f"Saved Folder: <code>{escape(ospath.dirname(source_path))}</code>\n"
        f"Torrent Path: <code>{escape(torrent_path)}</code>\n"
        f"Trackers: <code>{len(trackers)}</code>\n"
        f"Mode: <code>{'Private' if Config.CTORRENT_PRIVATE else 'Public'}</code>"
    )
    await send_file(message, torrent_path, caption)


async def _send_folder_artifacts(message, folder_path, torrent_path, trackers):
    screenshots_dir = ospath.join(folder_path, "Screenshots")
    await makedirs(screenshots_dir, exist_ok=True)
    videos = []
    for name in sorted(await listdir(folder_path)):
        path = ospath.join(folder_path, name)
        if await aiopath.isfile(path) and ospath.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
            videos.append(path)

    for video in videos:
        title, _, _ = format_clean_poster_title(ospath.basename(video))
        title = title or ospath.splitext(ospath.basename(video))[0]
        summary = await _probe_summary(video)
        sheet = await _create_contact_sheet_with_header(
            video, summary, title, screenshots_dir
        )
        if sheet:
            await send_file(
                message,
                sheet,
                f"Contact sheet: <code>{escape(ospath.basename(video))}</code>",
            )

    caption = (
        "<b>Created Folder Torrent</b>\n"
        f"Folder: <code>{escape(ospath.basename(folder_path))}</code>\n"
        f"Files: <code>{len(videos)}</code>\n"
        f"Torrent Path: <code>{escape(torrent_path)}</code>\n"
        f"Trackers: <code>{len(trackers)}</code>\n"
        f"Mode: <code>{'Private' if Config.CTORRENT_PRIVATE else 'Public'}</code>"
    )
    await send_file(message, torrent_path, caption)


@new_task
async def create_torrent(_, message):
    status_msg = await send_message(message, "Create Torrent: preparing source...")
    try:
        folder_mode = _parse_folder_mode(message)
        if folder_mode:
            folder_name, links = folder_mode
            if not links:
                await edit_message(
                    status_msg,
                    "Folder mode needs links: <code>/ctorrent 'Folder Name' - link1 link2</code>",
                )
                return
            storage_dir = str(Config.CTORRENT_STORAGE_DIR or "/usr/src/app/torrents/seeding")
            folder_path = ospath.join(storage_dir, folder_name)
            await makedirs(folder_path, exist_ok=True)
            for index, link in enumerate(links, start=1):
                await _edit_progress(
                    status_msg,
                    f"Create Torrent: downloading folder source {index}/{len(links)}...",
                    extra=f"Folder: <code>{escape(folder_name)}</code>",
                )
                await _download_link(link, folder_path, status_msg)
            await _edit_progress(
                status_msg,
                "Create Torrent: generating folder contact sheets and torrent...",
                extra=f"Folder: <code>{escape(folder_name)}</code>",
            )
            torrent_path, trackers = await _make_torrent(folder_path)
            await _send_folder_artifacts(message, folder_path, torrent_path, trackers)
            await edit_message(status_msg, "Create Torrent folder mode: completed.")
            return

        source_path = await _resolve_source(message, status_msg)
        if not source_path:
            await edit_message(
                status_msg,
                "Reply to a Telegram video/file or send <code>/ctorrent direct_link</code>.",
            )
            return
        await _edit_progress(
            status_msg,
            "Create Torrent: generating thumbnail, contact sheet, and torrent...",
            extra=f"Source: <code>{escape(ospath.basename(source_path))}</code>",
        )
        torrent_path, trackers = await _make_torrent(source_path)
        await _send_artifacts(message, source_path, torrent_path, trackers)
        await edit_message(status_msg, "Create Torrent: completed.")
    except Exception as e:
        await edit_message(status_msg, f"Create Torrent failed:\n<code>{escape(str(e))}</code>")
