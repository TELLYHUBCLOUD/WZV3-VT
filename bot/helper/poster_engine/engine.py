from html import escape
from io import BytesIO
from os import path as ospath
from re import IGNORECASE, findall, search, sub
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
    _looks_like_anime_name,
    apply_caption_word_replace,
    build_caption_metadata,
    choose_media_title_seed,
    extract_metadata_from_filename,
    get_final_poster_url,
    get_video_thumbnail,
)

POSTER_SIZE = (1280, 720)
TMDB_IMAGE = "https://image.tmdb.org/t/p/{size}{path}"
POSTER_TEMPLATE_COUNT = 8


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


class _SafeCaptionDict(dict):
    def __missing__(self, key):
        return ""


def _clean_search_title(filename, extracted=""):
    candidates = [extracted, filename]
    for candidate in candidates:
        text = _safe_text(candidate)
        if not text:
            continue
        text = ospath.splitext(text)[0]
        text = sub(r"\[@[^\]]+\]", " ", text)
        text = sub(r"^\[(?!S\d{1,2}\s*E\d{1,4}\])[^]]+\]\s*", " ", text, flags=IGNORECASE)
        text = sub(r"^(?:AS|A S|ANIME[ _.-]*STARFALL|STARFALL)[\s._:-]+", " ", text, flags=IGNORECASE)
        text = sub(r"^[^\w\[]+", " ", text)
        text = sub(r"\s+", " ", text).strip(" -_.")
        cleaned = _clean_title_from_filename(text)
        # Folder/archive names often end at a standalone season tag (S01).
        # It is useful template metadata, but poisons provider title searches.
        cleaned = sub(
            r"(?i)\b(?:S(?:eason)?\s*0*\d{1,2})\b",
            " ",
            cleaned,
        )
        cleaned = sub(r"\s+", " ", cleaned).strip(" -_.")
        if len(findall(r"[A-Za-z0-9]", cleaned)) >= 2:
            return cleaned
    return _clean_title_from_filename(filename)


def _missing(value):
    return value in (None, "", "N/A", "None", "n/a")


def _merge_missing(base, extra):
    for key, value in (extra or {}).items():
        if _missing(value):
            continue
        if _missing(base.get(key)):
            base[key] = value
    return base


def _rating_number(value):
    text = _safe_text(value)
    match = search(r"(\d+(?:\.\d+)?)", text)
    return match.group(1) if match else ""


def _short_plot(data):
    return data.get("plot") or data.get("synopsis") or data.get("genres") or ""


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


def _genre_list(data, limit=3):
    raw = _safe_text(data.get("genres"))
    raw = raw.replace("#", "").replace("_", " ")
    parts = [p.strip(" ,") for p in raw.split(",") if p.strip(" ,")]
    if not parts and raw:
        parts = raw.split()[:limit]
    return parts[:limit]


def _top_nav(draw, items, x=350, y=46, fill=(255, 255, 255), accent=(229, 45, 230)):
    for idx, item in enumerate(items[:3]):
        tx = x + idx * 150
        draw.text((tx, y), item.upper(), font=_font(18, True), fill=fill)
        if idx == 0:
            draw.line((tx, y + 26, tx + 110, y + 26), fill=accent, width=3)


def _rating_label(data):
    rating = _rating_number(data.get("rating"))
    return f"RATING: {rating}" if rating else "RATING"


def _meta_line(data):
    bits = [
        data.get("studio"),
        data.get("category", "").upper(),
        data.get("year"),
    ]
    return " - ".join(_safe_text(x) for x in bits if _safe_text(x))


async def _download_image(url):
    if not url:
        return None
    url = str(url).strip()
    if not url.startswith(("http://", "https://")):
        LOGGER.warning(f"Poster image URL is not HTTP(S); ignoring: {url[:80]}")
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
        if year:
            wanted_year = str(year)
            item = next(
                (
                    result
                    for result in results
                    if str(
                        result.get("release_date")
                        or result.get("first_air_date")
                        or ""
                    ).startswith(wanted_year)
                ),
                results[0],
            )
        else:
            item = results[0]
        media_type = item.get("media_type") or "movie"
        details = {}
        try:
            async with AsyncClient(timeout=12, headers=headers) as client:
                detail_res = await client.get(
                    f"https://api.themoviedb.org/3/{media_type}/{item.get('id')}",
                    params={"language": "en-US"},
                )
            if detail_res.status_code == 200:
                details = detail_res.json()
        except Exception:
            details = {}
        genres = ", ".join(g.get("name", "") for g in details.get("genres", []) if g.get("name"))
        studio = ""
        if companies := details.get("production_companies"):
            studio = companies[0].get("name") or ""
        return {
            "provider": "TMDb",
            "category": "tv" if media_type == "tv" else "movie",
            "title": item.get("title") or item.get("name") or title,
            "name": item.get("title") or item.get("name") or title,
            "year": (item.get("release_date") or item.get("first_air_date") or "")[:4],
            "plot": item.get("overview") or "",
            "synopsis": item.get("overview") or "",
            "rating": f"{float(item.get('vote_average') or 0):.1f}" if item.get("vote_average") else "",
            "status": details.get("status") or "",
            "genres": genres,
            "studio": studio,
            "first_aired": details.get("first_air_date") or details.get("release_date") or "",
            "landscape_url": _tmdb_url(item.get("backdrop_path"), "w1280"),
            "portrait_url": _tmdb_url(item.get("poster_path"), "w780"),
            "poster_url": _tmdb_url(item.get("poster_path"), "w780"),
        }
    except Exception as err:
        LOGGER.warning(f"TMDb poster search failed for '{title}': {err}")
        return {}


async def _anime_search(title):
    query = """
    query ($search: String!) {
      Page(page: 1, perPage: 5) {
        media(search: $search, type: ANIME) {
          id
          title { english romaji native }
          bannerImage
          coverImage { extraLarge large }
          description(asHtml: false)
          genres
          seasonYear
          averageScore
          status
          episodes
          startDate { year month day }
          studios(isMain: true) { nodes { name } }
        }
      }
    }
    """
    media = {}
    try:
        async with AsyncClient(timeout=12) as client:
            res = await client.post(
                "https://graphql.anilist.co",
                json={"query": query, "variables": {"search": title}},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 StarFallX/1.2",
                },
            )
        if res.status_code == 200:
            results = (
                res.json()
                .get("data", {})
                .get("Page", {})
                .get("media")
                or []
            )
            media = next((item for item in results if item.get("bannerImage")), None)
            media = media or (results[0] if results else {})
    except Exception as err:
        LOGGER.warning(f"AniList poster search failed for '{title}': {err}")
    if not media:
        return {}
    names = media.get("title") or {}
    name = names.get("english") or names.get("romaji") or names.get("native") or title
    cover = media.get("coverImage") or {}
    genres = ", ".join(media.get("genres") or [])
    score = media.get("averageScore")
    rating = f"{score / 10:.1f}/10 - AniList" if score else ""
    start = media.get("startDate") or {}
    first_aired = "-".join(
        str(start.get(k)).zfill(2 if k != "year" else 4)
        for k in ("year", "month", "day")
        if start.get(k)
    )
    studios = ((media.get("studios") or {}).get("nodes") or [])
    studio = studios[0].get("name") if studios else ""
    return {
        "provider": "AniList",
        "category": "anime",
        "title": name,
        "name": name,
        "year": str(media.get("seasonYear") or ""),
        "plot": sub(r"<.*?>", "", media.get("description") or ""),
        "synopsis": sub(r"<.*?>", "", media.get("description") or ""),
        "rating": rating,
        "status": str(media.get("status") or "").replace("_", " ").title(),
        "episodes": str(media.get("episodes") or ""),
        "studio": studio,
        "first_aired": first_aired,
        "genres": genres,
        "landscape_url": media.get("bannerImage") or "",
        "portrait_url": cover.get("extraLarge") or cover.get("large") or "",
        "poster_url": cover.get("extraLarge") or cover.get("large") or media.get("bannerImage") or "",
    }


async def _imdb_search(title, year=None):
    try:
        from ...modules.imdb import get_poster

        data = await sync_to_async(get_poster, title, bulk=False, id=False, file=None)
        if not data:
            return {}
        return {
            "provider": "IMDb",
            "category": "tv" if data.get("kind") == "Series" else "movie",
            "title": data.get("title") or title,
            "name": data.get("title") or title,
            "year": data.get("year") or year or "",
            "plot": data.get("plot") or data.get("storyline") or "",
            "synopsis": data.get("plot") or data.get("storyline") or "",
            "rating": data.get("rating") or "",
            "status": data.get("kind") or "",
            "genres": ", ".join(data.get("genres") or []) if isinstance(data.get("genres"), list) else data.get("genres") or "",
            "studio": data.get("production") or "",
            "first_aired": data.get("release_date") or "",
            "landscape_url": "",
            "portrait_url": data.get("poster") or "",
            "poster_url": data.get("poster") or "",
        }
    except Exception as err:
        LOGGER.warning(f"IMDb poster search failed for '{title}': {err}")
        return {}


async def _metadata(
    filename,
    filepath=None,
    user_dict=None,
    file_caption="",
    link="",
    first_file="",
    custom_name="",
    merge_source_name="",
):
    seed = choose_media_title_seed(
        filename,
        first_file=first_file,
        file_caption=file_caption,
        custom_name=custom_name,
        link=link,
        merge_source_name=merge_source_name,
        source_filename=filename,
    )
    caption_data = await build_caption_metadata(
        filename,
        filepath,
        source_filename=filename,
        first_file=first_file,
        file_caption=file_caption,
        custom_name=custom_name,
        link=link,
        merge_source_name=merge_source_name,
    )
    base = dict(caption_data)
    title = _clean_search_title(seed, base.get("title") or "")
    if not title or title.lower() == "unknown":
        title = _clean_search_title(seed)
    anime_hint = _looks_like_anime_name(seed, title)

    provider = {}
    if anime_hint:
        provider = await _anime_search(title)
    if not provider:
        provider = await _tmdb_search(title, base.get("year"))
    if not provider and not anime_hint:
        provider = await _anime_search(title)
    if not provider and anime_hint:
        provider = await _anime_search(title)

    tv_hint = bool(
        search(
            r"(?i)(?:\bS\d{1,2}(?:\s*E\d{1,4})?\b|\bseason\s*\d+\b|\bepisode\s*\d+\b)",
            seed,
        )
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
        "episodes": base.get("episodes") or base.get("episode", ""),
        "range": base.get("range", ""),
        "start": base.get("start", ""),
        "end": base.get("end", ""),
        "genres": "",
        "rating": "",
        "status": "",
        "studio": "",
        "first_aired": "",
        "plot": "",
        "synopsis": "",
        "quality": base.get("quality", ""),
        "resolution": base.get("resolution", ""),
        "bit": base.get("bit", ""),
        "codec": base.get("codec") or base.get("vcodec", ""),
        "audio": base.get("audio", ""),
        "language": base.get("language", ""),
        "languages": base.get("languages", ""),
        "audio_codec": base.get("audio_codec", ""),
        "audio_channels": base.get("audio_channels", ""),
        "audio_bitrate": base.get("audio_bitrate", ""),
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
    for key, value in caption_data.items():
        if not data.get(key):
            data[key] = value
    for key in (
        "language",
        "languages",
        "audio_codec",
        "audio_channels",
        "audio_bitrate",
        "subtitles",
        "shortlang",
        "shortsub",
        "range",
        "start",
        "end",
    ):
        if caption_data.get(key):
            data[key] = caption_data[key]
    if data.get("start") and data.get("end") and not data.get("range"):
        data["range"] = f"EP({data['start']}-{data['end']})"
    if data.get("range") and (
        not data.get("episodes") or data.get("episodes") == data.get("episode")
    ):
        data["episodes"] = data["range"].removeprefix("EP(").removesuffix(")")
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
    canvas = _cover(bg, POSTER_SIZE).filter(ImageFilter.GaussianBlur(9))
    canvas = _overlay_gradient(canvas, 205, 95)
    draw = ImageDraw.Draw(canvas)
    _paste_logo(canvas, logo)
    _top_nav(draw, _genre_list(data) or ["Completed", "Adventure", "Fantasy"])
    _draw_wrapped(draw, (58, 260), _safe_text(data.get("title")).upper(), _font(62, True), "white", 690, 8, 2)
    draw.text((60, 332), _meta_line(data).upper(), font=_font(26, True), fill=(255, 255, 255))
    draw.rounded_rectangle((40, 400, 640, 662), radius=30, fill=(60, 64, 70))
    draw.rounded_rectangle((60, 425, 220, 485), radius=10, fill=(173, 31, 209))
    draw.text((70, 446), "DOWNLOAD!!", font=_font(22, True), fill="white")
    draw.rounded_rectangle((380, 425, 600, 485), radius=10, fill=(173, 31, 209))
    draw.text((442, 446), _rating_label(data), font=_font(22, True), fill="white")
    draw.polygon([(402, 452), (416, 452), (421, 435), (427, 452), (443, 452), (431, 462), (436, 478), (421, 468), (406, 478), (412, 462)], fill=(38, 226, 57))
    _draw_wrapped(draw, (60, 515), _short_plot(data), _font(24, True), (220, 220, 220), 540, 6, 5)
    _paste_rounded(canvas, side, (824, 86, 398, 590), 46, 7)
    draw.text((585, 688), _safe_text(data.get("brand"), "Anime Starfall"), font=_font(18, True), fill=(205, 205, 205))
    return canvas


def _template_three(bg, side, data, logo):
    canvas = Image.new("RGB", POSTER_SIZE, (247, 248, 252))
    draw = ImageDraw.Draw(canvas)
    accent = (247, 105, 110)
    draw.ellipse((640, 88, 1200, 650), fill=accent)
    draw.ellipse((1110, -70, 1255, 75), fill=accent)
    _paste_logo(canvas, logo)
    brand = _safe_text(data.get("brand"), "Anime Starfall").upper().split()
    draw.text((70, 52), brand[0] if brand else "ANIME", font=_font(20, True), fill=(24, 28, 34))
    draw.text((142, 52), " ".join(brand[1:]) or "STARFALL", font=_font(20, True), fill=accent)
    _top_nav(draw, ["Main", "Ongoing", "Finished"], x=350, y=55, fill=(155, 158, 164), accent=accent)
    meta = " - ".join(x for x in (data.get("category", "").upper(), data.get("year"), f"{data.get('episodes')} EPISODES" if data.get("episodes") else "") if x)
    draw.text((70, 165), meta, font=_font(20), fill=(160, 160, 160))
    _draw_wrapped(draw, (70, 210), _safe_text(data.get("title")).upper(), _font(36, True), (24, 28, 34), 520, 9, 3)
    draw.line((70, 320, 70, 393), fill=accent, width=2)
    _draw_wrapped(draw, (94, 322), _short_plot(data), _font(18), (145, 145, 145), 460, 6, 4)
    draw.rounded_rectangle((70, 470, 310, 520), radius=24, outline=accent, width=2)
    draw.rectangle((70, 470, 120, 520), fill=accent)
    draw.text((150, 490), "DOWNLOAD NOW", font=_font(18, True), fill=(85, 85, 85))
    draw.text((70, 635), "STUDIO", font=_font(15, True), fill=accent)
    draw.text((210, 635), "FIRST AIRED", font=_font(15, True), fill=accent)
    draw.text((350, 635), "RATING", font=_font(15, True), fill=accent)
    draw.text((70, 662), _safe_text(data.get("studio"), "N/A")[:18], font=_font(15), fill=(35, 35, 35))
    draw.text((210, 662), _safe_text(data.get("first_aired") or data.get("year"), "N/A")[:18], font=_font(15), fill=(35, 35, 35))
    draw.text((350, 662), _safe_text(data.get("rating"), "N/A")[:18], font=_font(15), fill=(35, 35, 35))
    _paste_rounded(canvas, side, (705, 78, 415, 610), 4, None)
    return canvas


def _template_four(bg, side, data, logo):
    canvas = Image.new("RGB", POSTER_SIZE, (237, 248, 255))
    draw = ImageDraw.Draw(canvas)
    blue = (83, 188, 232)
    draw.ellipse((-145, 0, 520, 700), fill=(226, 243, 252))
    draw.ellipse((632, -130, 1270, 800), fill=(186, 227, 247))
    draw.ellipse((-75, 350, 120, 550), outline=blue, width=8)
    _top_nav(draw, ["Episode", "Trailer", "Home"], x=150, y=68, fill=(31, 41, 55), accent=blue)
    _paste_logo(canvas, logo)
    draw.text((150, 195), _safe_text(data.get("brand"), "Anime Starfall").upper(), font=_font(20, True), fill=(31, 41, 55))
    if data.get("rating"):
        draw.text((320, 195), _rating_number(data.get("rating")) + "/10", font=_font(20, True), fill=(31, 41, 55))
    _draw_wrapped(draw, (150, 245), _safe_text(data.get("title")).upper(), _font(42, True), (24, 34, 48), 520, 8, 3)
    _draw_wrapped(draw, (150, 345), _short_plot(data), _font(23), (60, 70, 82), 450, 8, 4)
    draw.rounded_rectangle((150, 470, 350, 530), radius=6, fill=blue)
    draw.text((190, 493), "Watch Now", font=_font(22, True), fill="white")
    _paste_rounded(canvas, side, (760, 70, 330, 610), 8, None)
    return canvas


def _template_five(bg, side, data, logo):
    canvas = Image.new("RGB", POSTER_SIZE, (8, 8, 8))
    poster = _cover(bg, (580, 720))
    canvas.paste(poster, (700, 0))
    draw = ImageDraw.Draw(canvas)
    _paste_logo(canvas, logo)
    _top_nav(draw, _genre_list(data) or ["Comedy", "Romance", "Slice Of Life"], x=345, y=22, fill="white", accent=(160, 77, 255))
    _draw_wrapped(draw, (60, 205), _safe_text(data.get("title")).upper(), _font(50, True), "white", 570, 14, 3)
    draw.rounded_rectangle((50, 335, 650, 475), radius=10, fill=(35, 35, 35))
    _draw_wrapped(draw, (70, 355), _short_plot(data), _font(20, True), "white", 545, 5, 5)
    draw.rounded_rectangle((60, 505, 280, 565), radius=28, fill=(95, 150, 255))
    draw.rounded_rectangle((170, 505, 280, 565), radius=28, fill=(164, 77, 255))
    draw.text((100, 527), "WATCH NOW!!", font=_font(22, True), fill="white")
    draw.ellipse((60, 640, 108, 688), outline="white", width=3)
    draw.text((125, 658), _safe_text(data.get("brand"), "Anime Starfall"), font=_font(17, True), fill="white")
    return canvas


def _template_six(bg, side, data, logo):
    canvas = Image.new("RGB", POSTER_SIZE, (248, 248, 247))
    left = _cover(bg, (550, 720))
    canvas.paste(left, (0, 0))
    shade = Image.new("RGBA", (550, 720), (0, 0, 0, 70))
    canvas.paste(Image.alpha_composite(left.convert("RGBA"), shade).convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(canvas)
    _draw_brand(draw, data.get("brand"))
    _paste_logo(canvas, logo)
    x = 600
    _draw_wrapped(draw, (x, 215), _safe_text(data.get("title")).upper(), _font(46, True), (0, 0, 0), 590, 8, 3)
    chips = _genre_list(data, 2)
    cx = x
    for chip in chips:
        width = min(135, 20 + len(chip) * 10)
        draw.rounded_rectangle((cx, 324, cx + width, 354), radius=15, outline=(120, 120, 120), width=1)
        draw.text((cx + 15, 332), chip.title(), font=_font(14, True), fill=(80, 80, 80))
        cx += width + 12
    if rating := _rating_number(data.get("rating")):
        draw.text((cx + 6, 326), f"Avg Rating: {rating}", font=_font(20, True), fill=(0, 0, 0))
    draw.line((x, 376, 1230, 376), fill=(210, 210, 210), width=2)
    _draw_wrapped(draw, (x, 405), _short_plot(data).upper(), _font(19), (120, 120, 120), 590, 8, 6)
    draw.line((x, 560, 1230, 560), fill=(210, 210, 210), width=2)
    draw.rounded_rectangle((x, 585, x + 150, 630), radius=22, fill=(0, 0, 0))
    draw.text((x + 24, 600), "WATCH NOW!!", font=_font(16, True), fill="white")
    draw.ellipse((x + 165, 585, x + 210, 630), fill=(0, 0, 0))
    return canvas


def _template_seven(bg, side, data, logo):
    canvas = _cover(bg, POSTER_SIZE).filter(ImageFilter.GaussianBlur(8))
    shade = Image.new("RGBA", POSTER_SIZE, (0, 0, 0, 112))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), shade).convert("RGB")
    draw = ImageDraw.Draw(canvas)
    brand = _safe_text(data.get("brand"), "Anime Starfall").upper()
    genres = _genre_list(data, 3) or ["Movie", "HD", "Release"]
    draw.text((58, 48), brand, font=_font(18, True), fill=(255, 255, 255))
    _top_nav(draw, genres, x=345, y=42, fill=(255, 255, 255), accent=(210, 160, 35))
    panel = Image.new("RGBA", (620, 270), (255, 255, 255, 42))
    panel = panel.filter(ImageFilter.GaussianBlur(0.2))
    canvas.paste(panel.convert("RGB"), (48, 390))
    draw.rounded_rectangle((48, 390, 668, 660), radius=28, outline=(255, 255, 255, 62), width=2)
    _draw_wrapped(draw, (60, 230), _safe_text(data.get("title")).upper(), _font(54, True), "white", 650, 10, 3)
    draw.text((60, 330), _meta_line(data).upper(), font=_font(24, True), fill=(240, 240, 240))
    draw.rounded_rectangle((86, 430, 238, 485), radius=8, fill=(185, 135, 35))
    draw.text((116, 448), "DOWNLOAD", font=_font(18, True), fill="white")
    if rating := _rating_number(data.get("rating")):
        draw.rounded_rectangle((285, 430, 455, 485), radius=8, fill=(130, 82, 34))
        draw.text((315, 448), f"IMDb {rating}", font=_font(18, True), fill="white")
    _draw_wrapped(draw, (78, 518), _short_plot(data), _font(20), (245, 245, 245), 535, 7, 4)
    _paste_rounded(canvas, side, (820, 86, 330, 500), 16, 6)
    _paste_logo(canvas, logo)
    return canvas


def _template_eight(bg, side, data, logo):
    canvas = Image.new("RGB", POSTER_SIZE, (236, 226, 226))
    draw = ImageDraw.Draw(canvas)
    accent = (160, 96, 128)
    draw.rectangle((0, 0, 365, 720), fill=accent)
    for i in range(-120, 110, 18):
        draw.line((i, 0, i + 150, 150), fill=(0, 0, 0), width=5)
    draw.rounded_rectangle((500, 24, 850, 82), radius=28, outline=(0, 0, 0), width=2)
    draw.ellipse((524, 36, 570, 82), outline=(0, 0, 0), width=4)
    draw.line((560, 72, 584, 96), fill=(0, 0, 0), width=4)
    draw.text((602, 43), _safe_text(data.get("brand"), "Anime Starfall").upper(), font=_font(22, True), fill=(0, 0, 0))
    draw.text((926, 30), "HOME", font=_font(24, True), fill=(0, 0, 0))
    draw.rounded_rectangle((1024, 20, 1142, 62), radius=20, fill=(0, 0, 0))
    draw.text((1047, 32), "ANIME", font=_font(22, True), fill=(255, 255, 255))
    draw.text((1170, 30), "MOVIE", font=_font(24, True), fill=(0, 0, 0))
    _paste_rounded(canvas, side, (72, 72, 360, 540), 20, None)
    _draw_wrapped(draw, (500, 192), _safe_text(data.get("title")).upper(), _font(42, True), (0, 0, 0), 610, 8, 2)
    cx = 500
    for genre in _genre_list(data, 3) or ["Action", "Adventure", "Fantasy"]:
        w = max(130, min(185, 34 + len(genre) * 12))
        draw.rounded_rectangle((cx, 314, cx + w, 358), radius=22, fill=(0, 0, 0))
        draw.text((cx + 28, 326), genre.upper(), font=_font(18, True), fill=(255, 255, 255))
        cx += w + 30
    draw.text((500, 392), "SYNOPSIS :", font=_font(24, True), fill=(0, 0, 0))
    _draw_wrapped(draw, (500, 420), _short_plot(data), _font(18, True), (0, 0, 0), 600, 4, 7)
    for y in range(270, 535, 46):
        draw.ellipse((1142, y, 1176, y + 34), fill=(190, 220, 230), outline=accent, width=3)
    draw.ellipse((500, 634, 530, 664), fill=(0, 0, 0))
    draw.ellipse((542, 634, 572, 664), fill=(0, 0, 0))
    draw.ellipse((584, 634, 614, 664), fill=(0, 0, 0))
    draw.text((720, 632), _safe_text(data.get("brand"), "Anime Starfall").upper(), font=_font(28, True), fill=(0, 0, 0))
    _paste_logo(canvas, logo)
    return canvas


def _render_template(style, bg, side, data, logo=None):
    style = str(style or "1")
    renderers = {
        "1": _template_one,
        "2": _template_two,
        "3": _template_three,
        "4": _template_four,
        "5": _template_five,
        "6": _template_six,
        "7": _template_seven,
        "8": _template_eight,
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
    values = _SafeCaptionDict(
        {k: escape(_safe_text(v), quote=False) for k, v in metadata.items()}
    )
    values.setdefault("name", values.get("title", ""))
    try:
        caption = template.format_map(values)
        return apply_caption_word_replace(
            caption, (user_dict or {}).get("CAPTION_WORD_REPLACE", "")
        )
    except Exception as err:
        LOGGER.warning(f"Poster caption format failed: {err}")
        return f"<b>{values.get('title') or values.get('name') or 'Poster'}</b>"


async def generate_task_poster(
    filename,
    filepath,
    user_id,
    user_dict=None,
    file_caption="",
    first_file="",
    custom_name="",
    link="",
    merge_source_name="",
    as_doc=False,
):
    user_dict = user_dict or {}
    if not is_auto_poster_enabled(user_dict):
        return None
    metadata = await _metadata(
        filename,
        filepath,
        user_dict,
        file_caption,
        link,
        first_file,
        custom_name,
        merge_source_name,
    )
    template = str(_cfg(user_dict, "POST_TEMPLATE_ID", 1) or 1)
    if template not in {str(i) for i in range(1, POSTER_TEMPLATE_COUNT + 1)}:
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
