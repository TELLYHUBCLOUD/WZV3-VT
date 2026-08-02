from html import escape
from difflib import SequenceMatcher
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
POSTER_TEMPLATE_COUNT = 10


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
    words = _safe_text(text).replace("\n", " \n ").split()
    if not words:
        return []
    lines = []
    line = ""
    consumed = 0
    for word in words:
        if word == "\n":
            if line:
                lines.append(line)
                line = ""
            if len(lines) >= max_lines:
                break
            consumed += 1
            continue
        if draw.textbbox((0, 0), word, font=font)[2] > width:
            chunks = []
            chunk = ""
            for char in word:
                trial = chunk + char
                if chunk and draw.textbbox((0, 0), trial, font=font)[2] > width:
                    chunks.append(chunk)
                    chunk = char
                else:
                    chunk = trial
            if chunk:
                chunks.append(chunk)
        else:
            chunks = [word]
        for chunk in chunks:
            trial = f"{line} {chunk}".strip()
            if draw.textbbox((0, 0), trial, font=font)[2] <= width:
                line = trial
                continue
            if line:
                lines.append(line)
            line = chunk
            if len(lines) >= max_lines:
                break
        consumed += 1
        if len(lines) >= max_lines:
            break
    if line and len(lines) < max_lines:
        lines.append(line)
    if lines and (consumed < len(words) or len(lines) > max_lines):
        lines = lines[:max_lines]
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


def _paste_logo(canvas, logo_img, position=(1150, 42), size=(74, 74)):
    if not logo_img:
        return
    logo = _contain(logo_img, size)
    canvas.paste(logo, position)


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
    source = _safe_text(data.get("rating_source"), "IMDb")
    return f"{source}: {rating}" if rating else ""


def _normalize_title(value):
    return sub(r"[^a-z0-9]+", " ", _safe_text(value).lower()).strip()


def _title_similarity(left, right):
    left = _normalize_title(left)
    right = _normalize_title(right)
    if not left or not right:
        return 0.0
    ratio = SequenceMatcher(None, left, right).ratio()
    left_words = set(left.split())
    right_words = set(right.split())
    overlap = len(left_words & right_words) / max(len(left_words | right_words), 1)
    return max(ratio, overlap)


def _imdb_matches(provider, imdb):
    if not imdb:
        return False
    if _title_similarity(provider.get("title"), imdb.get("title")) < 0.64:
        return False
    provider_year = search(r"\d{4}", _safe_text(provider.get("year")))
    imdb_year = search(r"\d{4}", _safe_text(imdb.get("year")))
    if provider_year and imdb_year:
        if abs(int(provider_year.group()) - int(imdb_year.group())) > 1:
            return False
    return True


def _rating_info(data):
    value = _rating_number(data.get("imdb_rating") or data.get("rating"))
    source = _safe_text(data.get("rating_source"))
    if not source:
        provider = _safe_text(data.get("provider"))
        source = "AniList" if provider == "AniList" else "TMDb" if provider == "TMDb" else "IMDb"
    try:
        number = max(0.0, min(float(value), 10.0))
    except (TypeError, ValueError):
        return "", source, 0
    return f"{number:.1f}", source, max(0, min(5, round(number / 2)))


def _draw_rating(draw, xy, data, fill="white", accent=(244, 183, 46), size=20):
    value, source, stars = _rating_info(data)
    if not value:
        return xy[0]
    x, y = xy
    label = f"{source} {value}"
    font = _font(size, True)
    draw.text((x, y), label, font=font, fill=fill)
    x += draw.textbbox((x, y), label, font=font)[2] - x + 14
    star_font = _font(max(15, size - 2), True)
    draw.text((x, y), "★" * stars + "☆" * (5 - stars), font=star_font, fill=accent)
    return x + draw.textbbox((x, y), "★★★★★", font=star_font)[2] - x


def _split_genres(data, limit=3):
    return [genre.title() for genre in _genre_list(data, limit)]


def _draw_chips(draw, genres, box, fill, text_fill="white", max_items=3):
    x, y, width, height = box
    cursor = x
    font = _font(max(13, min(17, height // 2)), True)
    for genre in list(genres)[:max_items]:
        text = _safe_text(genre)
        if not text:
            continue
        text_width = draw.textbbox((0, 0), text, font=font)[2]
        chip_width = min(max(text_width + 28, 90), 190)
        if cursor + chip_width > x + width:
            break
        draw.rounded_rectangle(
            (cursor, y, cursor + chip_width, y + height),
            radius=height // 2,
            fill=fill,
        )
        draw.text((cursor + 14, y + max(3, (height - font.size) // 2 - 1)), text, font=font, fill=text_fill)
        cursor += chip_width + 10
    return cursor


def _fit_text(draw, text, box, max_size, min_size=14, bold=False, max_lines=3, line_gap=6):
    _, _, width, height = box
    text = _safe_text(text)
    if not text:
        return _font(min_size, bold), []
    for size in range(max_size, min_size - 1, -2):
        font = _font(size, bold)
        lines = _wrap_text(draw, text, font, width, max_lines=max_lines)
        used_height = len(lines) * size + max(0, len(lines) - 1) * line_gap
        if lines and used_height <= height:
            return font, lines
    font = _font(min_size, bold)
    return font, _wrap_text(draw, text, font, width, max_lines=max_lines)


def _draw_text_box(
    draw,
    box,
    text,
    fill,
    max_size,
    min_size=14,
    bold=False,
    max_lines=3,
    line_gap=6,
    anchor="la",
):
    x, y, _, _ = box
    font, lines = _fit_text(
        draw,
        text,
        box,
        max_size,
        min_size=min_size,
        bold=bold,
        max_lines=max_lines,
        line_gap=line_gap,
    )
    cursor = y
    for line in lines:
        draw.text((x, cursor), line, font=font, fill=fill, anchor=anchor)
        cursor += font.size + line_gap
    return cursor


def _glass_panel(canvas, box, fill=(15, 18, 24, 190), radius=24, blur=8):
    x, y, w, h = box
    crop = canvas.crop((x, y, x + w, y + h)).filter(ImageFilter.GaussianBlur(blur)).convert("RGBA")
    tint = Image.new("RGBA", (w, h), fill)
    panel = Image.alpha_composite(crop, tint)
    mask = _round_rect_mask((w, h), radius)
    canvas.paste(panel.convert("RGB"), (x, y), mask)


def _landscape_canvas(image):
    image = image.convert("RGB")
    ratio = image.width / max(image.height, 1)
    if ratio >= 1.45:
        return _cover(image, POSTER_SIZE)
    background = _cover(image, POSTER_SIZE).filter(ImageFilter.GaussianBlur(22))
    fitted = ImageOps.contain(image, (640, 680), method=Image.Resampling.LANCZOS)
    x = (POSTER_SIZE[0] - fitted.width) // 2
    y = (POSTER_SIZE[1] - fitted.height) // 2
    background.paste(fitted, (x, y))
    return background


def _scene_crop(image, index, size=(160, 90)):
    image = _cover(image, (640, 360))
    offsets = ((0, 0), (240, 70), (480, 150))
    x, y = offsets[index % len(offsets)]
    x = min(x, image.width - size[0])
    y = min(y, image.height - size[1])
    return image.crop((x, y, x + size[0], y + size[1])).resize(size, Image.Resampling.LANCZOS)


def _paste_vertical_label(canvas, text, position, font_size=26, fill="white"):
    font = _font(font_size, True)
    text = _safe_text(text).upper()
    if not text:
        return
    bounds = font.getbbox(text)
    layer = Image.new("RGBA", (bounds[2] + 20, bounds[3] - bounds[1] + 20), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((10, 10 - bounds[1]), text, font=font, fill=fill)
    layer = layer.rotate(90, expand=True, resample=Image.Resampling.BICUBIC)
    canvas.paste(layer, position, layer)


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
        wanted_year = search(r"\d{4}", _safe_text(year))

        def result_score(result):
            names = (
                result.get("title"),
                result.get("name"),
                result.get("original_title"),
                result.get("original_name"),
            )
            similarity = max(_title_similarity(title, name) for name in names if name)
            result_year = search(
                r"\d{4}",
                _safe_text(result.get("release_date") or result.get("first_air_date")),
            )
            year_score = 0.0
            if wanted_year and result_year:
                distance = abs(int(wanted_year.group()) - int(result_year.group()))
                year_score = 0.18 if distance == 0 else 0.08 if distance == 1 else -0.2
            return similarity + year_score

        item = max(results, key=result_score)
        if result_score(item) < 0.5:
            return {}
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
            def media_score(item):
                names = (item.get("title") or {}).values()
                similarity = max(
                    (_title_similarity(title, name) for name in names if name),
                    default=0.0,
                )
                return similarity + (0.04 if item.get("bannerImage") else 0.0)

            media = max(results, key=media_score) if results else {}
            if media and media_score(media) < 0.5:
                media = {}
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

        query = f"{title} {year}" if year and not search(r"\b\d{4}\b", title) else title
        data = await sync_to_async(get_poster, query, bulk=False, id=False, file=None)
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

    imdb = await _imdb_search(
        provider.get("title") or title,
        provider.get("year") or base.get("year"),
    )
    if not _imdb_matches(provider or {"title": title, "year": base.get("year")}, imdb):
        imdb = {}

    # AniList is the preferred anime artwork provider. When it lacks one of the
    # two aspect ratios, supplement only the missing art from a validated TMDb
    # result, then use IMDb portrait art as the final provider fallback.
    if provider.get("provider") == "AniList" and (
        not provider.get("landscape_url") or not provider.get("portrait_url")
    ):
        tmdb_art = await _tmdb_search(
            provider.get("title") or title,
            provider.get("year") or base.get("year"),
        )
        if _imdb_matches(provider, tmdb_art):
            for key in ("landscape_url", "portrait_url", "poster_url"):
                if not provider.get(key) and tmdb_art.get(key):
                    provider[key] = tmdb_art[key]
    if imdb and not provider:
        provider = dict(imdb)
    if imdb:
        for key in ("portrait_url", "poster_url"):
            if not provider.get(key) and imdb.get(key):
                provider[key] = imdb[key]
        if not provider.get("landscape_url") and imdb.get("poster_url"):
            provider["landscape_url"] = imdb["poster_url"]

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
        "rating_source": "",
        "imdb_rating": "",
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
    if provider.get("rating"):
        data["rating_source"] = provider.get("provider") or ""
    if imdb:
        imdb_rating = _rating_number(imdb.get("rating"))
        if imdb_rating:
            data["imdb_rating"] = imdb_rating
            data["rating"] = imdb_rating
            data["rating_source"] = "IMDb"
        for key in ("year", "genres", "studio", "first_aired", "status"):
            if _missing(data.get(key)) and not _missing(imdb.get(key)):
                data[key] = imdb[key]
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
        portrait = landscape.copy()
    return _landscape_canvas(landscape), portrait


async def _logo(user_dict):
    logo = _cfg(user_dict, "POST_LOGO", "")
    if logo and await aiopath.exists(str(logo)):
        try:
            return await sync_to_async(Image.open, str(logo))
        except Exception:
            return None
    return await _download_image(logo) if logo else None


def _template_one(bg, side, data, logo):
    canvas = _overlay_gradient(_cover(bg, POSTER_SIZE), 230, 45)
    draw = ImageDraw.Draw(canvas)
    _draw_brand(draw, data.get("brand"))
    _paste_logo(canvas, logo)
    accent = (224, 167, 45)
    _draw_text_box(draw, (58, 128, 455, 210), data.get("title"), "white", 68, 36, True, 3, 8)
    _draw_chips(draw, _split_genres(data), (58, 350, 455, 38), (108, 77, 42), max_items=2)
    meta = " • ".join(value for value in (_safe_text(data.get("year")), _safe_text(data.get("status"))) if value)
    if meta:
        draw.text((58, 402), meta.upper(), font=_font(20, True), fill=(230, 230, 230))
    _draw_rating(draw, (58, 434), data, fill="white", accent=accent, size=20)
    _draw_text_box(draw, (58, 470, 430, 82), _short_plot(data), (235, 235, 235), 22, 16, False, 3, 5)
    draw.rectangle((58, 570, 220, 615), fill=(135, 131, 126))
    draw.rectangle((238, 570, 400, 615), fill=(135, 131, 126))
    draw.text((86, 582), "DOWNLOAD", font=_font(19, True), fill="white")
    draw.text((266, 582), "MORE INFO", font=_font(19, True), fill="white")
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
    _draw_text_box(draw, (58, 165, 690, 170), _safe_text(data.get("title")).upper(), "white", 62, 34, True, 3, 7)
    meta = " • ".join(value for value in (_safe_text(data.get("studio")), _safe_text(data.get("year"))) if value)
    if meta:
        draw.text((60, 350), meta.upper(), font=_font(21, True), fill=(240, 240, 240))
    _glass_panel(canvas, (40, 390, 640, 272), fill=(35, 39, 47, 185), radius=30, blur=5)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((60, 415, 225, 470), radius=10, fill=(173, 31, 209))
    draw.text((76, 432), "DOWNLOAD", font=_font(20, True), fill="white")
    _draw_rating(draw, (380, 430), data, fill="white", accent=(39, 226, 57), size=20)
    _draw_text_box(draw, (60, 505, 570, 120), _short_plot(data), (224, 224, 224), 23, 16, True, 5, 5)
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
    _draw_text_box(draw, (70, 205, 520, 125), _safe_text(data.get("title")).upper(), (24, 28, 34), 38, 25, True, 3, 6)
    draw.line((70, 345, 70, 452), fill=accent, width=3)
    _draw_text_box(draw, (94, 345, 455, 105), _short_plot(data), (125, 125, 125), 19, 15, False, 5, 5)
    draw.rounded_rectangle((70, 480, 310, 530), radius=24, outline=accent, width=2)
    draw.rectangle((70, 480, 120, 530), fill=accent)
    draw.text((150, 497), "DOWNLOAD NOW", font=_font(18, True), fill=(85, 85, 85))
    _draw_rating(draw, (70, 565), data, fill=(40, 40, 40), accent=accent, size=18)
    facts = [
        ("STUDIO", data.get("studio")),
        ("FIRST AIRED", data.get("first_aired") or data.get("year")),
        ("RATING", _rating_label(data)),
    ]
    fact_x = 70
    for label, value in ((label, value) for label, value in facts if _safe_text(value)):
        draw.text((fact_x, 635), label, font=_font(15, True), fill=accent)
        draw.text(
            (fact_x, 662),
            _safe_text(value)[:22],
            font=_font(15),
            fill=(35, 35, 35),
        )
        fact_x += 170
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
    draw.text((150, 180), _safe_text(data.get("brand"), "Anime Starfall").upper(), font=_font(20, True), fill=(31, 41, 55))
    _draw_rating(draw, (150, 212), data, fill=(31, 41, 55), accent=blue, size=18)
    _draw_text_box(draw, (150, 250, 520, 145), _safe_text(data.get("title")).upper(), (24, 34, 48), 43, 27, True, 3, 7)
    _draw_text_box(draw, (150, 410, 455, 105), _short_plot(data), (60, 70, 82), 22, 16, False, 4, 6)
    _draw_chips(draw, _split_genres(data), (150, 535, 500, 36), blue, max_items=3)
    draw.rounded_rectangle((150, 595, 350, 650), radius=6, fill=blue)
    draw.text((190, 612), "Watch Now", font=_font(22, True), fill="white")
    _paste_rounded(canvas, side, (760, 70, 330, 610), 8, None)
    return canvas


def _template_five(bg, side, data, logo):
    canvas = Image.new("RGB", POSTER_SIZE, (8, 8, 8))
    poster = _cover(bg, (580, 720))
    canvas.paste(poster, (700, 0))
    fade = Image.new("RGBA", (240, 720))
    fade_pixels = fade.load()
    for x in range(240):
        alpha = int(255 * (1 - x / 239))
        for y in range(720):
            fade_pixels[x, y] = (8, 8, 8, alpha)
    canvas.paste(fade.convert("RGB"), (700, 0), fade.getchannel("A"))
    draw = ImageDraw.Draw(canvas)
    _paste_logo(canvas, logo)
    _top_nav(draw, _genre_list(data) or ["Comedy", "Romance", "Slice Of Life"], x=345, y=22, fill="white", accent=(160, 77, 255))
    meta = " • ".join(value for value in (_safe_text(data.get("year")), _safe_text(data.get("status"))) if value)
    if meta:
        draw.text((60, 155), meta.upper(), font=_font(19, True), fill=(185, 150, 245))
    _draw_text_box(draw, (60, 195, 570, 155), _safe_text(data.get("title")).upper(), "white", 51, 29, True, 3, 9)
    draw.rounded_rectangle((50, 370, 650, 515), radius=10, fill=(35, 35, 35))
    _draw_text_box(draw, (70, 390, 545, 105), _short_plot(data), "white", 20, 15, True, 5, 5)
    _draw_rating(draw, (60, 540), data, fill="white", accent=(160, 77, 255), size=20)
    draw.rounded_rectangle((60, 575, 280, 635), radius=28, fill=(95, 150, 255))
    draw.rounded_rectangle((170, 575, 280, 635), radius=28, fill=(164, 77, 255))
    draw.text((100, 597), "WATCH NOW!!", font=_font(22, True), fill="white")
    draw.ellipse((60, 652, 100, 692), outline="white", width=3)
    draw.text((118, 664), _safe_text(data.get("brand"), "Anime Starfall"), font=_font(17, True), fill="white")
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
    meta = " • ".join(value for value in (_safe_text(data.get("year")), _safe_text(data.get("category"))) if value)
    if meta:
        draw.text((x, 165), meta.upper(), font=_font(18, True), fill=(105, 105, 105))
    _draw_text_box(draw, (x, 205, 590, 145), _safe_text(data.get("title")).upper(), (0, 0, 0), 46, 28, True, 3, 7)
    chips = _genre_list(data, 2)
    cx = x
    for chip in chips:
        width = min(135, 20 + len(chip) * 10)
        draw.rounded_rectangle((cx, 365, cx + width, 397), radius=16, outline=(120, 120, 120), width=1)
        draw.text((cx + 15, 373), chip.title(), font=_font(14, True), fill=(80, 80, 80))
        cx += width + 12
    _draw_rating(draw, (x, 415), data, fill=(0, 0, 0), accent=(238, 170, 35), size=18)
    draw.line((x, 452, 1230, 452), fill=(210, 210, 210), width=2)
    _draw_text_box(draw, (x, 480, 590, 105), _short_plot(data).upper(), (105, 105, 105), 19, 14, False, 5, 6)
    draw.line((x, 600, 1230, 600), fill=(210, 210, 210), width=2)
    draw.rounded_rectangle((x, 625, x + 150, 670), radius=22, fill=(0, 0, 0))
    draw.text((x + 24, 640), "WATCH NOW!!", font=_font(16, True), fill="white")
    draw.ellipse((x + 165, 625, x + 210, 670), fill=(0, 0, 0))
    return canvas


def _template_seven(bg, side, data, logo):
    canvas = Image.new("RGB", POSTER_SIZE, (250, 249, 247))
    draw = ImageDraw.Draw(canvas)
    maroon = (86, 31, 43)
    draw.rectangle((995, 0, 1280, 720), fill=maroon)
    draw.pieslice((-95, 600, 165, 860), 180, 360, fill=maroon)
    draw.pieslice((540, 405, 900, 765), 180, 360, fill=maroon)
    draw.text((34, 32), _safe_text(data.get("brand"), "Anime Starfall").upper(), font=_font(18, True), fill=(35, 35, 35))
    draw.text((125, 32), "HOME", font=_font(17, True), fill=(35, 35, 35))
    draw.text((235, 32), "ABOUT", font=_font(17, True), fill=(35, 35, 35))
    draw.text((345, 32), "NEWS", font=_font(17, True), fill=(35, 35, 35))
    _draw_text_box(draw, (34, 120, 650, 170), _safe_text(data.get("title")), (30, 30, 30), 58, 32, True, 3, 8)
    meta = " • ".join(value for value in (_safe_text(data.get("category")), _safe_text(data.get("year"))) if value)
    if meta:
        draw.text((38, 302), meta.upper(), font=_font(20, True), fill=maroon)
    _draw_chips(draw, _split_genres(data), (38, 345, 610, 38), maroon, max_items=3)
    _draw_rating(draw, (38, 405), data, fill=(35, 35, 35), accent=(217, 153, 44), size=20)
    _draw_text_box(draw, (38, 455, 620, 110), _short_plot(data), (55, 55, 55), 21, 15, False, 5, 5)
    draw.rounded_rectangle((185, 585, 402, 642), radius=7, outline=maroon, width=3)
    draw.text((226, 602), "GET STARTED  →", font=_font(18, True), fill=maroon)
    shadow = Image.new("RGBA", (450, 625), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((12, 12, 438, 613), radius=6, fill=(0, 0, 0, 85))
    canvas.paste(shadow, (747, 48), shadow)
    _paste_rounded(canvas, side, (765, 48, 420, 620), 4, 4)
    _paste_vertical_label(canvas, "ANIME VESSEL", (1210, 170), 26)
    _paste_logo(canvas, logo, position=(42, 650), size=(54, 54))
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
    _draw_text_box(draw, (500, 176, 610, 125), _safe_text(data.get("title")).upper(), (0, 0, 0), 42, 27, True, 3, 6)
    cx = 500
    for genre in _genre_list(data, 3) or ["Action", "Adventure", "Fantasy"]:
        w = max(130, min(185, 34 + len(genre) * 12))
        draw.rounded_rectangle((cx, 320, cx + w, 360), radius=20, fill=(0, 0, 0))
        draw.text((cx + 24, 331), genre.upper(), font=_font(17, True), fill=(255, 255, 255))
        cx += w + 30
    meta = " • ".join(value for value in (_safe_text(data.get("year")), _safe_text(data.get("status"))) if value)
    if meta:
        draw.text((500, 380), meta.upper(), font=_font(17, True), fill=accent)
    _draw_rating(draw, (500, 410), data, fill=(0, 0, 0), accent=accent, size=18)
    draw.text((500, 455), "SYNOPSIS :", font=_font(22, True), fill=(0, 0, 0))
    _draw_text_box(draw, (500, 488, 600, 108), _short_plot(data), (0, 0, 0), 18, 14, True, 6, 4)
    for y in range(270, 535, 46):
        draw.ellipse((1142, y, 1176, y + 34), fill=(190, 220, 230), outline=accent, width=3)
    draw.ellipse((500, 634, 530, 664), fill=(0, 0, 0))
    draw.ellipse((542, 634, 572, 664), fill=(0, 0, 0))
    draw.ellipse((584, 634, 614, 664), fill=(0, 0, 0))
    draw.text((720, 632), _safe_text(data.get("brand"), "Anime Starfall").upper(), font=_font(28, True), fill=(0, 0, 0))
    _paste_logo(canvas, logo)
    return canvas


def _template_nine(bg, side, data, logo):
    canvas = _cover(bg, POSTER_SIZE).filter(ImageFilter.GaussianBlur(7))
    shade = Image.new("RGBA", POSTER_SIZE, (12, 10, 8, 120))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), shade).convert("RGB")
    _glass_panel(canvas, (435, 45, 795, 630), fill=(24, 21, 18, 185), radius=18, blur=12)
    draw = ImageDraw.Draw(canvas)
    _paste_rounded(canvas, side, (40, 42, 375, 636), 16, 3)
    _paste_logo(canvas, logo, position=(60, 60), size=(58, 58))
    draw.text((475, 76), _safe_text(data.get("category"), "Movie").upper(), font=_font(19, True), fill=(210, 205, 195))
    meta = " • ".join(value for value in (_safe_text(data.get("year")), _safe_text(data.get("status"))) if value)
    if meta:
        draw.text((475, 108), meta.upper(), font=_font(19, True), fill=(210, 205, 195))
    _draw_text_box(draw, (475, 145, 620, 130), _safe_text(data.get("title")), "white", 54, 31, True, 3, 7)
    _draw_rating(draw, (1015, 82), data, fill="white", accent=(245, 174, 52), size=22)
    _draw_chips(draw, _split_genres(data), (475, 300, 690, 38), (62, 85, 72), max_items=3)
    _draw_text_box(draw, (475, 365, 690, 155), _short_plot(data), (226, 224, 218), 24, 17, False, 6, 7)
    draw.line((475, 548, 1165, 548), fill=(120, 112, 100), width=2)
    draw.text((475, 578), "PREMIUM RELEASE", font=_font(18, True), fill=(245, 174, 52))
    draw.text((475, 615), _safe_text(data.get("brand"), "Anime Starfall"), font=_font(25, True), fill="white")
    return canvas


def _template_ten(bg, side, data, logo):
    canvas = _cover(bg, POSTER_SIZE)
    canvas = _overlay_gradient(canvas, 245, 25)
    draw = ImageDraw.Draw(canvas)
    _paste_logo(canvas, logo, position=(78, 48), size=(70, 70))
    brand = _safe_text(data.get("brand"), "Anime Starfall").upper()
    draw.text((78, 142), brand, font=_font(18, True), fill=(206, 178, 235))
    meta = " • ".join(value for value in (_safe_text(data.get("category")), _safe_text(data.get("year"))) if value)
    if meta:
        draw.text((78, 180), meta.upper(), font=_font(20, True), fill=(206, 178, 235))
    _draw_text_box(draw, (78, 220, 540, 145), _safe_text(data.get("title")).upper(), "white", 64, 36, True, 3, 7)
    _draw_text_box(draw, (80, 380, 505, 82), _short_plot(data), (225, 219, 230), 20, 15, False, 4, 5)
    _draw_rating(draw, (80, 480), data, fill="white", accent=(246, 207, 57), size=22)
    _draw_chips(draw, _split_genres(data), (80, 525, 520, 38), (90, 54, 112), max_items=3)
    draw.rounded_rectangle((80, 585, 265, 638), radius=5, outline=(210, 188, 230), width=2)
    draw.text((112, 601), "WATCH NOW", font=_font(18, True), fill="white")
    for index in range(3):
        thumb = _scene_crop(side if index == 1 else bg, index, (135, 76))
        x = 650 + index * 155
        canvas.paste(thumb, (x, 596))
        draw.rounded_rectangle((x, 596, x + 135, 672), radius=7, outline=(235, 235, 235), width=2)
    draw.text((1120, 680), brand, font=_font(16, True), fill=(230, 230, 230), anchor="ra")
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
        "9": _template_nine,
        "10": _template_ten,
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
