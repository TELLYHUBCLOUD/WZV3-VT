from html import escape
from io import BytesIO
from os import path as ospath
from re import sub
from time import time

from aiofiles.os import makedirs
from httpx import AsyncClient
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ... import DOWNLOAD_DIR, LOGGER
from ...core.config_manager import Config
from .bot_utils import sync_to_async


def _font(size, bold=False):
    names = (
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "arialbd.ttf" if bold else "arial.ttf",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _cover_16x9(img, size=(1280, 720)):
    img = img.convert("RGB")
    scale = max(size[0] / img.width, size[1] / img.height)
    resized = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    left = max(0, (resized.width - size[0]) // 2)
    top = max(0, (resized.height - size[1]) // 2)
    return resized.crop((left, top, left + size[0], top + size[1]))


def _contain(img, box):
    img = img.convert("RGB")
    scale = min(box[0] / img.width, box[1] / img.height)
    return img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)


def _wrap_text(draw, text, font, max_width):
    words = str(text or "").split()
    lines = []
    line = ""
    for word in words:
        test = f"{line} {word}".strip()
        if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _render(metadata, image_bytes, output_path):
    src = Image.open(BytesIO(image_bytes)).convert("RGB")
    canvas = _cover_16x9(src).filter(ImageFilter.GaussianBlur(8))
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 110))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(canvas)

    panel = Image.new("RGBA", (610, 720), (0, 0, 0, 140))
    canvas.alpha_composite(panel, (0, 0))

    title = metadata.get("title") or "Hanime Video"
    episode = metadata.get("episode") or ""
    genres = metadata.get("genres") or "Hanime"
    brand = getattr(Config, "HANIME_BRAND_TEXT", "Anime Starfall") or "Anime Starfall"

    draw.text((58, 50), brand.upper(), fill=(255, 255, 255), font=_font(25, True))
    draw.line((58, 92, 448, 92), fill=(255, 255, 255), width=3)
    draw.text((58, 120), f"Episode {episode}" if episode else "Hanime", fill=(232, 190, 120), font=_font(28, True))

    title_font = _font(62, True)
    y = 170
    for line in _wrap_text(draw, title, title_font, 490)[:4]:
        draw.text((58, y), line, fill=(255, 255, 255), font=title_font)
        y += 68

    genre_lines = _wrap_text(draw, genres, _font(24), 490)[:3]
    y = max(y + 20, 500)
    for line in genre_lines:
        draw.text((58, y), line, fill=(220, 220, 220), font=_font(24))
        y += 32

    inset = _contain(src, (420, 620))
    x = 1280 - inset.width - 48
    y = max(48, (720 - inset.height) // 2)
    shadow = Image.new("RGBA", (inset.width + 18, inset.height + 18), (0, 0, 0, 115))
    canvas.alpha_composite(shadow, (x - 9, y + 9))
    canvas.paste(inset, (x, y))
    draw.rectangle((x, y, x + inset.width, y + inset.height), outline=(255, 255, 255), width=4)

    draw.text((1040, 650), brand, fill=(255, 255, 255), font=_font(25, True))
    canvas.convert("RGB").save(output_path, "JPEG", quality=93, optimize=True)
    return output_path


async def generate_hanime_poster(metadata):
    image_url = (
        metadata.get("cover_url")
        or metadata.get("poster_url")
        or metadata.get("thumbnail")
    )
    if not image_url:
        raise ValueError("Hanime metadata has no poster image.")
    async with AsyncClient(timeout=45, follow_redirects=True) as client:
        resp = await client.get(image_url)
    if resp.status_code != 200 or not resp.content:
        raise ValueError(f"Poster image returned HTTP {resp.status_code}.")
    out_dir = ospath.join(DOWNLOAD_DIR, "hanime-posters")
    await makedirs(out_dir, exist_ok=True)
    slug = sub(r"[^A-Za-z0-9_.-]+", "-", metadata.get("slug") or str(time())).strip("-")
    out_path = ospath.join(out_dir, f"{slug}-{int(time())}.jpg")
    return await sync_to_async(_render, metadata, resp.content, out_path)


def build_hanime_caption(metadata):
    synopsis = metadata.get("synopsis") or "N/A"
    if len(str(synopsis)) > 500:
        synopsis = f"{str(synopsis)[:497].rstrip()}..."
    values = {
        "title": metadata.get("title") or "Hanime Video",
        "episode": metadata.get("episode") or "",
        "views": metadata.get("views") or "N/A",
        "downloads": metadata.get("downloads") or "N/A",
        "rank": metadata.get("rank") or "N/A",
        "upload_date": metadata.get("upload_date") or "N/A",
        "studio": metadata.get("studio") or "N/A",
        "genres": metadata.get("genres") or "N/A",
        "synopsis": synopsis,
    }
    safe_values = {key: escape(str(value), quote=False) for key, value in values.items()}
    template = getattr(Config, "HANIME_POST_TEMPLATE", "") or "{title}"
    template = template.replace("{title} - {episode}", "{title}")
    template = template.replace("{title}-{episode}", "{title}")
    template = template.replace("Episode {episode}", "").replace("episode {episode}", "")
    try:
        return template.format_map(safe_values)
    except Exception as e:
        LOGGER.warning(f"Hanime caption template failed: {e}")
        return f"<b>{safe_values['title']}</b>"
