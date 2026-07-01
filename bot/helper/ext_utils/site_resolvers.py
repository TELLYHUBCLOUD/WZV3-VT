import hashlib
import os
import re
import tempfile
from asyncio import create_subprocess_exec, sleep, to_thread, wait_for
from logging import getLogger
from time import time
from urllib.parse import quote

from httpx import AsyncClient
from yt_dlp import YoutubeDL

from ...core.config_manager import Config

LOGGER = getLogger(__name__)

HANIME_RE = re.compile(r"https?://(?:www\.)?hanime\.tv/videos/hentai/([^/?#\s]+)", re.I)
MX_RE = re.compile(r"https?://(?:www\.)?(?:mxplayer\.in|mxplay\.com)/\S+", re.I)

HANIME_BASE = "https://cached.freeanimehentai.net/api/v8"
HANIME_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"
)
HANIME_HEADERS = {
    "User-Agent": HANIME_UA,
    "Referer": "https://hanime.tv/",
    "Origin": "https://hanime.tv",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "X-Csrf-Token": "",
    "X-License": "",
    "X-Session-Token": "",
    "X-User-License": "",
}

_hanime_vendor_cache = None
_hanime_video_cache = {}


def is_mx_link(link):
    return bool(MX_RE.search(str(link or "")))


def is_hanime_link(link):
    return bool(HANIME_RE.search(str(link or "")))


def is_supported_site(link):
    return is_mx_link(link) or is_hanime_link(link)


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
    if is_hanime_link(link):
        return await resolve_hanime(link, options)
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


def _hanime_slug(link):
    match = HANIME_RE.search(str(link or ""))
    return match.group(1) if match else ""


async def _hanime_api_resolve(link, api_base):
    async with AsyncClient(timeout=45) as client:
        resp = await client.get(api_base, params={"url": link})
        if resp.status_code != 200:
            raise ValueError(f"Hanime API returned HTTP {resp.status_code}.")
        data = resp.json()
    streams = data.get("streams") or []
    if not streams:
        raise ValueError("Hanime API returned no streams.")
    return data


def _hanime_headers(path):
    timestamp = int(time())
    sig = hashlib.sha1(f"{path}{timestamp}".encode()).hexdigest()
    return {
        **HANIME_HEADERS,
        "X-Signature-Version": "web2",
        "X-Time": str(timestamp),
        "X-Signature": sig,
    }


def _first_text(data, keys):
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (int, float)) and value:
                return str(value)
        for value in data.values():
            found = _first_text(value, keys)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _first_text(item, keys)
            if found:
                return found
    return ""


def _first_image(data):
    image_keys = (
        "cover_url",
        "poster_url",
        "thumbnail_url",
        "poster",
        "cover",
        "thumbnail",
        "image",
        "url",
    )
    if isinstance(data, dict):
        for key in image_keys:
            value = data.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        for value in data.values():
            found = _first_image(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _first_image(item)
            if found:
                return found
    return ""


def _hanime_title_from_data(slug, data):
    hv = data.get("hentai_video") if isinstance(data, dict) else {}
    hv = hv if isinstance(hv, dict) else {}
    slug_title = re.sub(r"[-_]+", " ", slug or "").strip()
    slug_title = re.sub(r"\s+", " ", slug_title).title()
    slug_episode = ""
    if slug_title:
        episode_match = re.search(r"\s+(\d{1,4})$", slug_title)
        if episode_match:
            slug_episode = episode_match.group(1)
            slug_title = slug_title[: episode_match.start()].strip()

    title = slug_title
    if not title:
        for key in ("name", "title", "display_name", "video_title", "slug"):
            value = hv.get(key) if hv else None
            if isinstance(value, str) and value.strip():
                title = re.sub(r"[-_]+", " ", value.strip()).title()
                break

    episode = ""
    for key in ("episode_number", "episode", "ep", "number"):
        value = hv.get(key) if hv else None
        if value not in (None, ""):
            episode = str(value).strip()
            break
    episode = episode or slug_episode
    if episode:
        episode = episode.zfill(2) if episode.isdigit() else episode
        title = re.sub(rf"(?i)\s+(?:ep(?:isode)?\s*)?0*{re.escape(episode)}$", "", title).strip()
        title = f"{title} - Episode {episode}"
    return title


async def _hanime_video_data(slug):
    if slug in _hanime_video_cache:
        return _hanime_video_cache[slug]
    async with AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{HANIME_BASE}/video",
            params={"id": slug},
            headers=_hanime_headers("/api/v8/video"),
        )
    if resp.status_code != 200:
        return {}
    data = resp.json()
    _hanime_video_cache[slug] = data
    return data


async def _hanime_get_hv_id(slug):
    data = await _hanime_video_data(slug)
    hv = data.get("hentai_video") or data
    vid = hv.get("id") or hv.get("hv_id")
    return str(vid) if vid else None


async def _hanime_vendor_script():
    global _hanime_vendor_cache
    if _hanime_vendor_cache:
        return _hanime_vendor_cache
    async with AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://hanime.tv/",
            headers={"User-Agent": HANIME_UA, "Accept": "text/html"},
        )
        match = re.search(r'src="(https://hanime-cdn\.com/js/vendor\.[^"]+)"', resp.text)
        if not match:
            return None
        resp = await client.get(
            match.group(1),
            headers={"User-Agent": HANIME_UA, "Referer": "https://hanime.tv/"},
        )
    if resp.status_code != 200:
        return None
    _hanime_vendor_cache = resp.text
    return _hanime_vendor_cache


async def _hanime_credentials():
    vendor_js = await _hanime_vendor_script()
    if not vendor_js:
        return None, None
    preamble = """
delete globalThis.process;
var window = new Proxy({
    top: { location: { origin: "https://hanime.tv" } },
    addEventListener: (e, cb) => {}
}, {
    set(o, k, v) {
        if (k == "ssignature" || k == "stime") console.log(k, v);
        o[k] = v;
        return true;
    }
});
globalThis.window = window;
"""
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as handle:
            handle.write(f"{preamble}\n{vendor_js}")
            tmp_path = handle.name
        proc = await create_subprocess_exec(
            "node",
            tmp_path,
            stdout=-1,
            stderr=-1,
        )
        out, _ = await wait_for(proc.communicate(), timeout=20)
        creds = {}
        for line in out.decode(errors="ignore").splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2:
                creds[parts[0]] = parts[1].strip()
        return creds.get("ssignature"), creds.get("stime")
    except FileNotFoundError:
        raise ValueError("Node.js is required for local Hanime resolver.")
    except Exception as e:
        LOGGER.warning(f"Hanime credential generation failed: {e}")
        return None, None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def _hanime_local_resolve(link):
    slug = _hanime_slug(link)
    if not slug:
        raise ValueError("Invalid Hanime link.")
    hv_id = await _hanime_get_hv_id(slug)
    if not hv_id:
        raise ValueError("Failed to resolve Hanime video id.")
    sig, timestamp = await _hanime_credentials()
    if not sig or not timestamp:
        raise ValueError("Failed to generate Hanime stream credentials.")
    async with AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"https://h.freeanimehentai.net/api/v8/guest/videos/{hv_id}/manifest",
            headers={
                **HANIME_HEADERS,
                "X-Signature": sig,
                "X-Signature-Version": "web2",
                "X-Time": timestamp,
            },
        )
    if resp.status_code != 200:
        raise ValueError(f"Hanime manifest returned HTTP {resp.status_code}.")
    data = resp.json()
    streams = []
    try:
        for server in data["videos_manifest"]["servers"]:
            for stream in server.get("streams", []):
                url = stream.get("url") or ""
                height = int(stream.get("height") or 0)
                if url.startswith("https://") and height:
                    streams.append(
                        {
                            "url": url,
                            "height": height,
                            "resolution": f"{stream.get('width', 0)}x{height}",
                            "filename": stream.get("filename") or "",
                        }
                    )
    except (KeyError, TypeError):
        pass
    if not streams:
        raise ValueError("No Hanime streams found.")
    video_data = await _hanime_video_data(slug)
    return {
        "slug": slug,
        "hv_id": hv_id,
        "title": _hanime_title_from_data(slug, video_data),
        "thumbnail": _first_image(video_data),
        "streams": streams,
    }


async def resolve_hanime(link, options=None):
    options = options or {}
    api_base = (
        options.get("hanime_api_base")
        or options.get("HANIME_API_BASE")
        or Config.HANIME_API_BASE
    )
    if not _is_internal_base(api_base):
        data = await _hanime_api_resolve(link, api_base)
    else:
        data = await _hanime_local_resolve(link)

    slug = data.get("slug") or _hanime_slug(link)
    if slug:
        video_data = {}
        if not data.get("title") or not data.get("thumbnail"):
            video_data = await _hanime_video_data(slug)
        data["title"] = _hanime_title_from_data(slug, video_data)
        if video_data:
            data["thumbnail"] = data.get("thumbnail") or _first_image(video_data)
    title = data.get("title") or slug.replace("-", " ").title() or "Hanime Video"
    streams = []
    for stream in data.get("streams") or []:
        url = stream.get("url") or ""
        try:
            height = int(stream.get("height") or 0)
        except Exception:
            height = 0
        if not url or not height:
            continue
        streams.append(
            {
                "url": url,
                "height": height,
                "label": f"{height}p",
                "resolution": stream.get("resolution") or "",
            }
        )
    streams = sorted(_unique_formats(streams), key=lambda item: item["height"], reverse=True)
    if not streams:
        raise ValueError("Hanime resolver returned no usable streams.")
    return {
        "type": "hanime",
        "source_url": link,
        "title": title,
        "thumbnail": data.get("thumbnail") or "",
        "streams": streams,
    }
