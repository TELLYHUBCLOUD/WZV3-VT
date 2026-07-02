import re
from asyncio import sleep, to_thread
from logging import getLogger
from urllib.parse import quote

from httpx import AsyncClient
from yt_dlp import YoutubeDL

from ...core.config_manager import Config

LOGGER = getLogger(__name__)

MX_RE = re.compile(r"https?://(?:www\.)?(?:mxplayer\.in|mxplay\.com)/\S+", re.I)


def is_mx_link(link):
    return bool(MX_RE.search(str(link or "")))


def is_supported_site(link):
    return is_mx_link(link)


def _fmt_size(size):
    if not size:
        return ""
    try:
        size = float(size)
    except Exception:
        return ""
    power = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < power:
            return f"{size:.1f}{unit}"
        size /= power
    return f"{size:.1f}PB"


def _unique_formats(items):
    seen = set()
    unique = []
    for item in items:
        key = item.get("id") or item.get("url")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _extract_formats(download_url):
    with YoutubeDL({"quiet": True, "nocheckcertificate": True}) as ydl:
        return ydl.extract_info(download_url, download=False) or {}


def _formats_from_info(info):
    videos = []
    audios = []
    for fmt in info.get("formats") or []:
        fid = str(fmt.get("format_id") or "").strip()
        if not fid:
            continue
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        height = fmt.get("height")
        ext = fmt.get("ext") or "mp4"
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        if vcodec and vcodec != "none":
            label = f"{height}p" if height else ext.upper()
            if fmt.get("fps"):
                label = f"{label}{fmt['fps']}"
            if size:
                label = f"{label} ({_fmt_size(size)})"
            videos.append(
                {
                    "id": fid,
                    "label": label,
                    "height": int(height or 0),
                    "size": size or 0,
                }
            )
        elif acodec and acodec != "none":
            lang = fmt.get("language") or fmt.get("format_note") or "Unknown"
            abr = fmt.get("abr")
            label = f"{int(abr)}kbps [{lang}]" if abr else f"{ext.upper()} [{lang}]"
            audios.append(
                {
                    "id": fid,
                    "label": label,
                    "language": str(lang),
                    "abr": abr or 0,
                }
            )
    return (
        sorted(_unique_formats(videos), key=lambda item: item["height"], reverse=True),
        _unique_formats(audios),
    )


def _is_internal_base(api_base):
    return str(api_base or "").strip().lower() in {"", "internal", "local", "builtin"}


async def resolve_external_site(link, options=None):
    options = options or {}
    if is_mx_link(link):
        return await resolve_mx(link, options)
    return None


async def resolve_mx(link, options=None):
    options = options or {}
    api_base = (
        options.get("mx_api_base")
        or options.get("MX_PLAYER_API_BASE")
        or Config.MX_PLAYER_API_BASE
    )
    if _is_internal_base(api_base):
        return await _resolve_mx_direct(link)

    api_url = (
        api_base.format(url=quote(link, safe=""))
        if "{url}" in api_base
        else f"{api_base.rstrip('/')}?url={quote(link, safe='')}"
    )
    data = None
    async with AsyncClient(timeout=30) as client:
        for attempt in range(3):
            try:
                resp = await client.get(api_url)
                if resp.status_code == 200:
                    data = resp.json()
                    break
                LOGGER.warning(f"MX resolver returned HTTP {resp.status_code}")
            except Exception as e:
                LOGGER.warning(f"MX resolver attempt {attempt + 1} failed: {e}")
            await sleep(1)

    if not data:
        raise ValueError("MX resolver did not return data.")
    if data.get("status") is False:
        raise ValueError(data.get("message") or "MX resolver failed.")

    download_url = data.get("m3u8_url") or data.get("mpd_url")
    if not download_url:
        raise ValueError("MX resolver did not return m3u8_url or mpd_url.")

    info = await to_thread(_extract_formats, download_url)
    videos, audios = _formats_from_info(info)
    if not videos and not audios:
        raise ValueError("MX formats were not readable by yt-dlp.")

    return {
        "type": "mx",
        "source_url": link,
        "download_url": download_url,
        "title": data.get("full_title") or data.get("title") or info.get("title") or "MX Player Video",
        "description": data.get("description") or "",
        "thumbnail": data.get("thumbnail") or info.get("thumbnail") or "",
        "videos": videos,
        "audios": audios,
    }


async def _resolve_mx_direct(link):
    info = await to_thread(_extract_formats, link)
    videos, audios = _formats_from_info(info)
    if not videos and not audios:
        raise ValueError("MX formats were not readable by yt-dlp internal resolver.")
    return {
        "type": "mx",
        "source_url": link,
        "download_url": link,
        "title": info.get("title") or info.get("fulltitle") or "MX Player Video",
        "description": info.get("description") or "",
        "thumbnail": info.get("thumbnail") or "",
        "videos": videos,
        "audios": audios,
    }
