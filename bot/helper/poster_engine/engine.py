from html import escape
from io import BytesIO
from os import path as ospath
from re import search, sub
from time import time

from aiofiles.os import makedirs
from aiofiles.os import path as aiopath
from httpx import AsyncClient
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from ... import DOWNLOAD_DIR, LOGGER
from ...core.config_manager import Config
from ..ext_utils.bot_utils import sync_to_async
from ..ext_utils.media_utils import (
    _clean_title_from_filename,
    _fetch_anilist_media,
    _looks_like_anime_name,
    build_caption_metadata,
    extract_metadata_from_filename,
    get_final_poster_url,
    get_video_thumbnail,
)

POSTER_SIZE = (1280, 720)
TMDB_IMAGE = "https://image.tmdb.org/t/p/{size}{path}"


def _bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def _cfg(user_dict, key, default=None):
    if user_dict and key in user_dict:
        return user_dict.get(key)
    return getattr(Config, key, default)


def is_auto_poster_enabled(user_dict):
    return _bool(_cfg(user_dict, "AUTO_POSTER_ENABLED", False), False)


def _safe_text(value, default=""):
    value = "" if value is None else str(value)
    return value.strip() or default


def _font(size, bold=False):
    names = (
        "arialbd.ttf" if bold else "arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _cover(img, size):
    return ImageOps.fit(img.convert("RGB"), size, method=Image.Resampling.LANCZOS)


def _contain(img, size):
    canvas = Image.new("RGB", size, (20, 20, 20))
    fitted = ImageOps.contain(img.convert("RGB"), size, method=Image.Resampling.LANCZOS)
    x = (size[0] - fitted.width) // 2
    y = (size[1] - fitted.height) // 2
    canvas.paste(fitted, (x, y))
    return canvas


def _round_rect_mask(size, radius):
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
    return mask


def _paste_rounded(base, img, box, radius=12, border=None):
    x, y, w, h = box
    img = _cover(img, (w, h)).convert("RGB")
    mask = _round_rect_mask((w, h), radius)
    if border:
        layer = Image.new("RGB", (w + border * 2, h + border * 2), (245, 245, 245))
        layer.paste(img, (border, border), mask)
        base.paste(layer, (x - border, y - border))
    else:
        base.paste(img, (x, y), mask)


def _overlay_gradient(base, left_alpha=190, right_alpha=60):
    w, h = base.size
    overlay = Image.new("RGBA", base.size)
    pix = overlay.load()
    for x in range(w):
        a = int(left_alpha + (right_alpha - left_alpha) * (x / max(w - 1, 1)))
        for y in range(h):
            pix[x, y] = (0, 0, 0, a)
    return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")


def _wrap_text(draw, text, font, width, max_lines=4):
    words = _safe_text(text).split()
    if not words:
        return []
    lines = []
    line = ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textbbox((0, 0), trial, font=font)[2] <= width:
            line = trial
            continue
        if line:
            lines.append(line)
        line = word
        if len(lines) >= max_lines:
            break
    if line and len(lines) < max_lines:
        lines.append(line)
    if len(lines) == max_lines and len(words) > 1:
        while lines[-1] and draw.textbbox((0, 0), lines[-1] + "...", font=font)[2] > width:
            lines[-1] = lines[-1][:-1].rstrip()
        lines[-1] += "..."
    return lines


def _draw_wrapped(draw, xy, text, font, fill, width, line_gap=8, max_lines=4):
    x, y = xy
    line_height = getattr(font, "size", 24)
    for line in _wrap_text(draw, text, font, width, max_lines=max_lines):
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height + line_gap
    return y


def _draw_brand(draw, brand):
    brand = _safe_text(brand, "Anime Starfall").upper()
    draw.text((58, 54), brand, font=_font(28, True), fill=(255, 255, 255))
    draw.line((58, 94, 430, 94), fill=(245, 245, 245), width=3)


def _paste_logo(canvas, logo_img):
    if not logo_img:
        return
    logo = _contain(logo_img, (74, 74))
    canvas.paste(logo, (1150, 42))


async def _download_image(url):
    if not url:
        return None
    try:
        async with AsyncClient(timeout=15, follow_redirects=True) as client:
            res = await client.get(url)
        if res.status_code != 200:
            return None
        return Image.open(BytesIO(res.content)).convert("RGB")
    except Exception as err:
        LOGGER.warning(f"Poster image download failed: {err}")
        return None


def _tmdb_url(path, size="w1280"):
    return TMDB_IMAGE.format(size=size, path=path) if path else ""


async def _tmdb_search(title, year=None):
    if not title or not Config.TMDB_ACCESS_TOKEN:
        return {}
    headers = {
        "Authorization": f"Bearer {Config.TMDB_ACCESS_TOKEN}",
        "accept": "application/json",
    }
    params = {
        "query": title,
        "include_adult": "false",
        "language": "en-US",
        "page": "1",
    }
    if year:
        params["year"] = year
    try:
        async with AsyncClient(timeout=12, headers=headers) as client:
            res = await client.get("https://api.themoviedb.org/3/search/multi", params=params)
        if res.status_code != 200:
            return {}
        results = [
            item
            for item in res.json().get("results", [])
            if item.get("media_type") in {"movie", "tv"}
        ]
        if not results:
            return {}
        item = results[0]
        media_type = item.get("media_type") or "movie"
        return {
            "provider": "TMDb",
            "category": "tv" if media_type == "tv" else "movie",
            "title": item.get("title") or item.get("name") or title,
            "name": item.get("title") or item.get("name") or title,
            "year": (item.get("release_date") or item.get("first_air_date") or "")[:4],
            "plot": item.get("overview") or "",
            "synopsis": item.get("overview") or "",
            "rating": f"{float(item.get('vote_average') or 0):.1f}" if item.get("vote_average") else "",
            "status": "",
            "genres": "",
            "landscape_url": _tmdb_url(item.get("backdrop_path"), "w1280"),
            "portrait_url": _tmdb_url(item.get("poster_path"), "w780"),
            "poster_url": _tmdb_url(item.get("poster_path"), "w780"),
        }
    except Exception as err:
        LOGGER.warning(f"TMDb poster search failed for '{title}': {err}")
        return {}


async def _anime_search(title):
    media = await _fetch_anilist_media(title)
    if not media:
        return {}
    names = media.get("title") or {}
    name = names.get("english") or names.get("romaji") or names.get("native") or title
    cover = media.get("coverImage") or {}
    genres = ", ".join(media.get("genres") or [])
    return {
        "provider": "AniList",
        "category": "anime",
        "title": name,
        "name": name,
        "year": str(media.get("seasonYear") or ""),
        "plot": sub(r"<.*?>", "", media.get("description") or ""),
        "synopsis": sub(r"<.*?>", "", media.get("description") or ""),
        "rating": "",
        "status": "",
        "genres": genres,
        "landscape_url": media.get("bannerImage") or "",
        "portrait_url": cover.get("extraLarge") or cover.get("large") or "",
        "poster_url": cover.get("extraLarge") or cover.get("large") or media.get("bannerImage") or "",
    }


async def _imdb_search(title, year=None):
    try:
        from ...modules.imdb import get_poster

        data = await sync_to_async(get_poster, title, bulk=True, id=False, file=None)
        if not data:
            return {}
        return {
            "provider": "IMDb",
            "category": "movie",
            "title": data.get("title") or title,
            "name": data.get("title") or title,
            "year": data.get("year") or year or "",
            "plot": data.get("plot") or data.get("storyline") or "",
            "synopsis": data.get("plot") or data.get("storyline") or "",
            "rating": data.get("rating") or "",
            "status": "",
            "genres": ", ".join(data.get("genres") or []) if isinstance(data.get("genres"), list) else data.get("genres") or "",
            "landscape_url": "",
            "portrait_url": data.get("poster") or "",
            "poster_url": data.get("poster") or "",
        }
    except Exception as err:
        LOGGER.warning(f"IMDb poster search failed for '{title}': {err}")
        return {}


async def _metadata(filename, filepath=None, user_dict=None, file_caption="", link=""):
    base = await extract_metadata_from_filename(filename, filepath)
    title = _clean_title_from_filename(base.get("title") or filename)
    if not title or title.lower() == "unknown":
        title = _clean_title_from_filename(filename)
    anime_hint = _looks_like_anime_name(filename, title)

    provider = {}
    if anime_hint:
        provider = await _anime_search(title)
    if not provider:
        provider = await _tmdb_search(title, base.get("year"))
    if not provider and not anime_hint:
        provider = await _anime_search(title)
    if not provider:
        provider = await _imdb_search(title, base.get("year"))

    tv_hint = bool(
        search(r"(?i)(?:\bS\d{1,2}\s*E\d{1,4}\b|\bseason\s*\d+\b|\bepisode\s*\d+\b)", filename)
    )
    data = {
        "provider": "",
        "category": "anime" if anime_hint else ("tv" if tv_hint else "movie"),
        "brand": _cfg(user_dict, "POST_BRAND_NAME", "Anime Starfall") or "Anime Starfall",
        "title": title,
        "name": title,
        "year": base.get("year", ""),
        "season": base.get("season", ""),
        "episode": base.get("episode", ""),
        "episodes": base.get("episode", ""),
        "genres": "",
        "rating": "",
        "status": "",
        "plot": "",
        "synopsis": "",
        "quality": base.get("quality", ""),
        "resolution": base.get("resolution", ""),
        "bit": base.get("bit", ""),
        "codec": base.get("codec") or base.get("vcodec", ""),
        "audio": base.get("audio", ""),
        "subtitles": "",
        "shortlang": "",
        "shortsub": base.get("shortsub", ""),
        "landscape_url": "",
        "portrait_url": "",
        "poster_url": "",
        "filename": filename,
        "link": link,
    }
    data.update({k: v for k, v in provider.items() if v not in (None, "")})
    caption_data = await build_caption_metadata(
        filename,
        filepath,
        file_caption=file_caption,
        link=link,
    )
    for key in (
        "quality",
        "resolution",
        "bit",
        "codec",
        "audio",
        "subtitles",
        "shortlang",
        "shortsub",
    ):
        if not data.get(key):
            data[key] = caption_data.get(key, "")
    return data


async def _images_for(metadata, filename, filepath=None, as_doc=False):
    landscape = await _download_image(metadata.get("landscape_url"))
    portrait = await _download_image(metadata.get("portrait_url") or metadata.get("poster_url"))
    if not landscape:
        fallback_url = await get_final_poster_url(filename, as_doc=as_doc)
        landscape = await _download_image(fallback_url)
        portrait = portrait or landscape
    if not landscape and filepath and await aiopath.exists(filepath):
        thumb = await get_video_thumbnail(filepath, 0)
        if thumb and await aiopath.exists(thumb):
            landscape = await sync_to_async(Image.open, thumb)
            landscape = landscape.convert("RGB")
    if not landscape:
        landscape = Image.new("RGB", POSTER_SIZE, (24, 26, 34))
    if not portrait:
        portrait = landscape
    return landscape, portrait


async def _logo(user_dict):
    logo = _cfg(user_dict, "POST_LOGO", "")
    if logo and await aiopath.exists(str(logo)):
        try:
            return await sync_to_async(Image.open, str(logo))
        except Exception:
            return None
    return await _download_image(logo) if logo else None


def _template_one(bg, side, data, logo):
    canvas = _cover(bg, POSTER_SIZE)
    canvas = _overlay_gradient(canvas, 220, 35)
    draw = ImageDraw.Draw(canvas)
    _draw_brand(draw, data.get("brand"))
    _paste_logo(canvas, logo)
    title_font = _font(70, True)
    small = _font(26)
    accent = (224, 167, 45)
    y = 145
    y = _draw_wrapped(draw, (58, y), data.get("title"), title_font, (255, 255, 255), 430, 10, 3)
    plot = data.get("plot") or data.get("synopsis") or data.get("genres")
    _draw_wrapped(draw, (72, y + 24), plot, small, (245, 245, 245), 410, 7, 5)
    draw.rectangle((72, 505, 235, 550), fill=(135, 131, 126))
    draw.rectangle((253, 505, 414, 550), fill=(135, 131, 126))
    draw.text((102, 517), "DOWNLOAD", font=_font(20, True), fill="white")
    draw.text((282, 517), "MORE INFO", font=_font(20, True), fill="white")
    _paste_rounded(canvas, side, (895, 92, 300, 480), 4, 6)
    draw.rectangle((540, 615, 1188, 680), fill=(139, 88, 33))
    draw.polygon([(540, 615), (720, 615), (690, 680), (510, 680)], fill=accent)
    draw.text((576, 640), "OVERVIEW", font=_font(22, True), fill="white")
    draw.text((755, 640), "STUDIO", font=_font(22, True), fill="white")
    draw.text((890, 640), "SEASON", font=_font(22, True), fill="white")
    draw.text((1030, 640), "RATINGS", font=_font(22, True), fill="white")
    return canvas


def _template_two(bg, side, data, logo):
    canvas = _cover(bg, POSTER_SIZE).filter(ImageFilter.GaussianBlur(7))
    canvas = _overlay_gradient(canvas, 210, 110)
    draw = ImageDraw.Draw(canvas)
    _draw_brand(draw, data.get("brand"))
    _paste_logo(canvas, logo)
    draw.text((58, 125), data.get("year") or "ANIME", font=_font(30, True), fill=(215, 215, 215))
    _draw_wrapped(draw, (58, 182), data.get("title"), _font(66, True), "white", 610, 8, 3)
    genres = data.get("genres") or data.get("quality") or ""
    _draw_wrapped(draw, (58, 505), genres, _font(28), (235, 235, 235), 500, 6, 4)
    _paste_rounded(canvas, side, (820, 88, 345, 500), 2, None)
    return canvas


def _template_three(bg, side, data, logo):
    canvas = _cover(bg, POSTER_SIZE)
    canvas = _overlay_gradient(canvas, 90, 90)
    draw = ImageDraw.Draw(canvas)
    _draw_brand(draw, data.get("brand"))
    _paste_logo(canvas, logo)
    _draw_wrapped(draw, (78, 395), data.get("title"), _font(64, True), "white", 650, 8, 3)
    draw.rounded_rectangle((58, 610, 1220, 690), radius=8, fill=(12, 16, 22))
    info = f"{data.get('year') or ''}   {data.get('genres') or ''}   {data.get('rating') or ''}"
    draw.text((88, 638), info.strip(), font=_font(26, True), fill=(235, 235, 235))
    _paste_rounded(canvas, side, (1000, 210, 170, 255), 6, 4)
    return canvas


def _template_four(bg, side, data, logo):
    canvas = _cover(bg, POSTER_SIZE).filter(ImageFilter.GaussianBlur(13))
    canvas = _overlay_gradient(canvas, 175, 120)
    draw = ImageDraw.Draw(canvas)
    _draw_brand(draw, data.get("brand"))
    _paste_logo(canvas, logo)
    draw.rounded_rectangle((88, 130, 1188, 628), radius=22, fill=(25, 28, 35))
    _paste_rounded(canvas, side, (130, 170, 260, 390), 12, None)
    _draw_wrapped(draw, (430, 170), data.get("title"), _font(58, True), "white", 685, 8, 3)
    meta = f"{data.get('year')}  {data.get('genres')}  {data.get('rating')}".strip()
    _draw_wrapped(draw, (430, 360), meta, _font(26, True), (218, 177, 91), 640, 6, 2)
    _draw_wrapped(draw, (430, 420), data.get("plot") or data.get("synopsis"), _font(25), (230, 230, 230), 650, 7, 5)
    return canvas


def _template_five(bg, side, data, logo):
    canvas = _cover(bg, POSTER_SIZE)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, 1280, 720), fill=(0, 0, 0, 82))
    _draw_brand(draw, data.get("brand"))
    _paste_logo(canvas, logo)
    draw.rounded_rectangle((58, 420, 750, 650), radius=10, fill=(8, 10, 14))
    _draw_wrapped(draw, (92, 455), data.get("title"), _font(58, True), "white", 600, 8, 2)
    info = " / ".join(x for x in (data.get("year"), data.get("genres"), data.get("rating")) if x)
    _draw_wrapped(draw, (94, 575), info, _font(25, True), (232, 201, 122), 580, 4, 2)
    _paste_rounded(canvas, side, (950, 100, 220, 330), 10, 4)
    return canvas


def _render_template(style, bg, side, data, logo=None):
    style = str(style or "1")
    renderers = {
        "1": _template_one,
        "2": _template_two,
        "3": _template_three,
        "4": _template_four,
        "5": _template_five,
    }
    return renderers.get(style, _template_one)(bg, side, data, logo)


async def search_poster_metadata(query, user_dict=None):
    query = _safe_text(query)
    return await _metadata(query, None, user_dict or {})


async def render_poster_option(metadata, user_id, user_dict=None, option="1", save_thumbnail=False):
    await makedirs("thumbnails", exist_ok=True)
    out_dir = "thumbnails" if save_thumbnail else ospath.join(DOWNLOAD_DIR, "poster_search")
    await makedirs(out_dir, exist_ok=True)
    path = (
        ospath.join("thumbnails", f"{user_id}.jpg")
        if save_thumbnail
        else ospath.join(out_dir, f"{user_id}_{option}_{time():.6f}.jpg")
    )
    bg, side = await _images_for(metadata, metadata.get("filename") or metadata.get("title") or "")
    logo = await _logo(user_dict or {})
    img = await sync_to_async(_render_template, option, bg, side, metadata, logo)
    await sync_to_async(img.save, path, "JPEG", quality=94, optimize=True)
    return path


def _caption_template(user_dict, category):
    key = {
        "anime": "POST_ANIME_CAPTION",
        "tv": "POST_TV_CAPTION",
    }.get(category, "POST_MOVIE_CAPTION")
    return _cfg(user_dict, key, "") or "{title}\n\n{plot}"


def build_post_caption(user_dict, metadata):
    template = _caption_template(user_dict or {}, metadata.get("category"))
    values = {k: escape(_safe_text(v), quote=False) for k, v in metadata.items()}
    values.setdefault("name", values.get("title", ""))
    try:
        return template.format_map(values)
    except Exception as err:
        LOGGER.warning(f"Poster caption format failed: {err}")
        return f"<b>{values.get('title') or values.get('name') or 'Poster'}</b>"


async def generate_task_poster(
    filename,
    filepath,
    user_id,
    user_dict=None,
    file_caption="",
    link="",
    as_doc=False,
):
    user_dict = user_dict or {}
    if not is_auto_poster_enabled(user_dict):
        return None
    metadata = await _metadata(filename, filepath, user_dict, file_caption, link)
    template = str(_cfg(user_dict, "POST_TEMPLATE_ID", 1) or 1)
    if template not in {"1", "2", "3", "4", "5"}:
        template = "1"
    await makedirs(ospath.join(DOWNLOAD_DIR, "generated_posters"), exist_ok=True)
    bg, side = await _images_for(metadata, filename, filepath, as_doc)
    logo = await _logo(user_dict)
    img = await sync_to_async(_render_template, template, bg, side, metadata, logo)
    path = ospath.join(DOWNLOAD_DIR, "generated_posters", f"{user_id}_{time():.6f}.jpg")
    await sync_to_async(img.save, path, "JPEG", quality=94, optimize=True)
    return {
        "path": path,
        "caption": build_post_caption(user_dict, metadata),
        "metadata": metadata,
        "template_id": template,
    }
