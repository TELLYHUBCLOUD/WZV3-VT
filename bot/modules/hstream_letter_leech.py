from asyncio import (
    CancelledError,
    FIRST_COMPLETED,
    Event,
    Lock,
    Semaphore,
    create_subprocess_exec,
    create_task,
    gather,
    sleep,
    wait,
)
from asyncio.subprocess import PIPE
from contextlib import suppress
from html import escape
from json import loads
from os import path as ospath
from pathlib import Path
from re import sub
from shutil import rmtree
from sys import executable

from httpx import AsyncClient
from PIL import Image, ImageFilter, ImageOps
from pyrogram.errors import FloodWait

from .. import DOWNLOAD_DIR, LOGGER, user_data
from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import cmd_exec, sync_to_async
from ..helper.ext_utils.hstream_resolver import HstreamResolver
from ..helper.ext_utils.media_utils import get_video_thumbnail
from ..helper.poster_engine.engine import POSTER_TEMPLATE_COUNT, render_poster_option
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import edit_message, send_message
from .batch_task_registry import BatchTaskController

_RUN_LOCK = Lock()
_DOWNLOAD_SLOTS = Semaphore(2)
_QUALITY_TIMEOUT = 45 * 60
_INVALID_FILENAME = r'[\\/:*?"<>|]'


def _parse_destination(message, tokens):
    destination = message.chat.id
    thread_id = (
        message.message_thread_id
        if getattr(message, "is_topic_message", False)
        else None
    )
    if "-up" not in tokens:
        return destination, thread_id
    index = tokens.index("-up")
    if index + 1 >= len(tokens):
        raise ValueError("<code>-up</code> requires CHAT_ID or CHAT_ID|TOPIC_ID")
    raw = tokens[index + 1].strip()
    if "|" in raw:
        raw, topic = raw.rsplit("|", 1)
        try:
            thread_id = int(topic)
        except ValueError as error:
            raise ValueError("Invalid topic id after <code>|</code>") from error
    try:
        destination = int(raw)
    except ValueError:
        destination = raw
    return destination, thread_id


def _safe_filename(value):
    value = sub(_INVALID_FILENAME, " ", str(value or ""))
    value = sub(r"\s+", " ", value).strip(" .-")
    return value[:180] or "Hstream"


def _fps_label(value):
    try:
        fps = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if not 40 <= fps <= 50.5:
        return ""
    return "48fps"


def _video_filename(episode, stream):
    title = _safe_filename(episode.title)[:110].rstrip(" .-")
    fps = _fps_label(stream.fps)
    media_details = " ".join(
        value
        for value in (stream.resolution, stream.codec, fps)
        if value
    )
    return _safe_filename(
        "🄰🅂- "
        f"{title} {media_details} "
        "[Japanese] ESub ~ [@Anime_Starfall🥰]"
    ) + ".mkv"


def _quality_summary(streams):
    def values(name):
        unique = []
        for stream in streams:
            value = getattr(stream, name)
            if value and value not in unique:
                unique.append(value)
        return "/".join(unique) or "N/A"

    return values("resolution"), values("bit"), values("codec")


def _genre_hashtags(genres):
    tags = []
    for genre in genres:
        tag = sub(r"[^a-z0-9]+", "_", str(genre).strip().lower()).strip("_")
        if tag and tag not in tags:
            tags.append(tag)
    return " ".join(f"#{tag}" for tag in tags) or "#anime"


def _poster_caption(episode):
    resolution, bit, codec = _quality_summary(episode.streams)
    title = escape(episode.title, quote=False)
    year = escape(episode.year, quote=False)
    views = f"{episode.views:,}" if episode.views else "N/A"
    genres = _genre_hashtags(episode.genres)
    description = escape(episode.description or "N/A", quote=False)
    fixed = (
        f"<b>「 {title}{f' - {year}' if year else ''} 」\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "╔════◇═══════════◇════\n"
        f"║ {views} Views\n"
        f"║ {escape(resolution)} {escape(bit)} {escape(codec)}\n"
        "║ Japanese ~ ESub\n"
        "╚════◇═══════════◇════\n\n"
        f"Genres : {genres}</b>\n\n"
        "<blockquote expandable><b>Synopsis :\n"
    )
    suffix = "</b></blockquote>\n\n<b>Dropped By ➤ [@Anime_Starfall🥰]</b>"
    allowance = max(0, 1024 - len(fixed) - len(suffix) - 3)
    if len(description) > allowance:
        description = description[:allowance].rsplit(" ", 1)[0] + "..."
    return fixed + description + suffix


async def _download_image(url, path, thumbnail=False):
    if not url:
        return ""
    try:
        async with AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()
        await sync_to_async(_save_image, response.content, path, thumbnail)
        return path
    except Exception as error:
        LOGGER.warning(f"Hstream artwork download failed: {error}")
        return ""


def _save_image(content, path, thumbnail):
    from io import BytesIO

    image = Image.open(BytesIO(content)).convert("RGB")
    if thumbnail:
        image = ImageOps.fit(image, (320, 180), Image.Resampling.LANCZOS)
        image = image.filter(ImageFilter.UnsharpMask(radius=1, percent=120, threshold=2))
        image.save(path, "JPEG", quality=94, optimize=True, subsampling=0)
    else:
        image = ImageOps.fit(image, (1280, 720), Image.Resampling.LANCZOS)
        image.save(path, "JPEG", quality=94, optimize=True, subsampling=0)


async def _download_subtitles(episode, directory):
    tracks = []
    if not episode.subtitles:
        return tracks
    async with AsyncClient(
        follow_redirects=True,
        timeout=30,
        verify=False,
        headers={"Referer": episode.source_url},
    ) as client:
        for index, subtitle in enumerate(episode.subtitles, start=1):
            try:
                response = await client.get(subtitle.url)
                response.raise_for_status()
                extension = ospath.splitext(subtitle.url.split("?", 1)[0])[1].lower()
                if extension not in {".ass", ".ssa", ".srt", ".vtt"}:
                    extension = ".ass"
                subtitle_path = ospath.join(directory, f"subtitle_{index}{extension}")
                Path(subtitle_path).write_bytes(response.content)
                tracks.append((subtitle.language, subtitle_path))
            except Exception as error:
                LOGGER.warning(f"Hstream subtitle download failed: {error}")
    return tracks


async def _download_sample_images(episode, directory):
    if not episode.sample_urls:
        return []
    sample_dir = ospath.join(directory, "samples")
    Path(sample_dir).mkdir(parents=True, exist_ok=True)
    slots = Semaphore(6)

    async with AsyncClient(
        follow_redirects=True,
        timeout=45,
        verify=False,
        headers={"Referer": episode.source_url},
    ) as client:

        async def download(index, url):
            async with slots:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    target = ospath.join(sample_dir, f"{index:03d}.jpg")
                    await sync_to_async(_save_sample_image, response.content, target)
                    return target
                except Exception as error:
                    LOGGER.warning(f"Hstream sample image failed for {url}: {error}")
                    return ""

        results = await gather(
            *(download(index, url) for index, url in enumerate(episode.sample_urls)),
        )
    return [path for path in results if path]


def _save_sample_image(content, target):
    from io import BytesIO

    with Image.open(BytesIO(content)) as image:
        image.convert("RGB").save(
            target,
            "JPEG",
            quality=92,
            optimize=True,
            subsampling=0,
        )


def _create_sample_collage(sample_paths, target):
    valid = []
    for sample_path in sample_paths:
        try:
            with Image.open(sample_path) as image:
                image.verify()
            valid.append(sample_path)
        except (OSError, ValueError):
            continue
    if not valid:
        return ""

    count = len(valid)
    if count <= 4:
        row_counts = [count]
    else:
        full_rows, remainder = divmod(count, 4)
        row_counts = [4] * full_rows
        if remainder == 1 and full_rows:
            row_counts[-1:] = [3, 2]
        elif remainder == 2 and full_rows:
            row_counts[-1:] = [3, 3]
        elif remainder:
            row_counts.append(remainder)
    row_heights = [round((1280 / columns) * 9 / 16) for columns in row_counts]
    canvas = Image.new("RGB", (1280, sum(row_heights)), "black")
    sample_index = 0
    top = 0
    for columns, row_height in zip(row_counts, row_heights, strict=True):
        tile_width = 1280 // columns
        for column in range(columns):
            sample_path = valid[sample_index]
            with Image.open(sample_path) as image:
                tile = ImageOps.fit(
                    image.convert("RGB"),
                    (tile_width, row_height),
                    Image.Resampling.LANCZOS,
                )
                canvas.paste(tile, (column * tile_width, top))
            sample_index += 1
        top += row_height
    canvas.save(target, "JPEG", quality=91, optimize=True, subsampling=0)
    return target


async def _video_info(video_path):
    result = await cmd_exec(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            video_path,
        ]
    )
    if result[2] != 0 or not result[0]:
        return {"duration": 0, "width": 0, "height": 0}
    try:
        payload = loads(result[0])
        video = next(
            (
                stream
                for stream in payload.get("streams", [])
                if stream.get("codec_type") == "video"
            ),
            {},
        )
        duration = round(float(payload.get("format", {}).get("duration") or 0))
        return {
            "duration": duration,
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
        }
    except (TypeError, ValueError):
        return {"duration": 0, "width": 0, "height": 0}


def _ass_time(seconds):
    seconds = max(0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remaining = seconds % 60
    return f"{hours}:{minutes:02}:{remaining:05.2f}"


def _write_brand_subtitle(path, duration):
    end = _ass_time(max(duration, 1) + 1)
    text = "Join our Telegram channel [@Anime_Starfall🥰]"
    content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Starfall,Arial,54,&H00FFFFFF,&H00FF9E2D,1,3,1,2,40,40,70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,{end},Starfall,,0,0,0,,{{\\b1}}{text}
"""
    Path(path).write_text(content, encoding="utf-8")
    return path


async def _stop_process(process):
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    with suppress(Exception):
        await process.wait()
    if process.returncode is None:
        process.kill()
        with suppress(Exception):
            await process.wait()


async def _run_ytdlp(url, output_dir, source_url, cancel_event, processes):
    template = ospath.join(output_dir, "source.%(ext)s")
    command = [
        executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--retries",
        "5",
        "--fragment-retries",
        "5",
        "--socket-timeout",
        "30",
        "--no-check-certificates",
        "--concurrent-fragments",
        "4",
        "--merge-output-format",
        "mkv",
        "--remux-video",
        "mkv",
        "--referer",
        source_url,
        "-f",
        "bv*+ba/b",
        "-o",
        template,
    ]
    if ospath.isfile("cookies.txt"):
        command.extend(("--cookies", "cookies.txt"))
    command.append(url)
    process = await create_subprocess_exec(*command, stdout=PIPE, stderr=PIPE)
    processes.add(process)
    communicate = create_task(process.communicate())
    cancelled = create_task(cancel_event.wait())
    try:
        done, _ = await wait(
            (communicate, cancelled),
            timeout=_QUALITY_TIMEOUT,
            return_when=FIRST_COMPLETED,
        )
        if cancelled in done or cancel_event.is_set():
            await _stop_process(process)
            communicate.cancel()
            with suppress(CancelledError):
                await communicate
            raise RuntimeError("Hstream run cancelled")
        if communicate not in done:
            await _stop_process(process)
            communicate.cancel()
            with suppress(CancelledError):
                await communicate
            raise TimeoutError("Hstream quality download timed out")
        _, stderr = await communicate
        if process.returncode:
            tail = stderr.decode(errors="ignore")[-1200:]
            raise RuntimeError(tail or f"yt-dlp exited with {process.returncode}")
    finally:
        processes.discard(process)
        cancelled.cancel()
        with suppress(CancelledError):
            await cancelled
    files = [
        item
        for item in Path(output_dir).glob("source.*")
        if item.is_file() and not item.name.endswith((".part", ".ytdl"))
    ]
    if not files:
        raise RuntimeError("yt-dlp completed without an output file")
    return str(max(files, key=lambda item: item.stat().st_size))


async def _remux_to_mkv(
    source,
    target,
    episode,
    subtitle_tracks,
    cancel_event,
    processes,
):
    info = await _video_info(source)
    brand_path = ospath.join(ospath.dirname(target), "starfall_brand.ass")
    await sync_to_async(_write_brand_subtitle, brand_path, info["duration"])
    inputs = [brand_path]
    inputs.extend(path for _, path in subtitle_tracks if ospath.isfile(path))
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source,
    ]
    for subtitle_path in inputs:
        command.extend(("-i", subtitle_path))
    command.extend(("-map", "0:v", "-map", "0:a?"))
    for input_index in range(1, len(inputs) + 1):
        command.extend(("-map", f"{input_index}:0"))
    metadata = {
        "title": episode.title,
        "artist": "@Anime_Starfall🥰",
        "album": "@Anime_Starfall🥰",
        "album_artist": "@Anime_Starfall🥰",
        "composer": "@Anime_Starfall🥰",
        "publisher": "@Anime_Starfall🥰",
        "copyright": "@Anime_Starfall🥰",
        "comment": "Encoded by @Anime_Starfall🥰",
        "description": episode.description or episode.title,
        "synopsis": episode.description or episode.title,
    }
    command.extend(("-c", "copy", "-c:s", "ass", "-metadata:s:a", "language=jpn"))
    for key, value in metadata.items():
        command.extend(("-metadata", f"{key}={value}"))
    command.extend(
        (
            "-metadata:s:s:0",
            "language=eng",
            "-metadata:s:s:0",
            "title=Anime Starfall",
            "-disposition:s:0",
            "default",
        )
    )
    for index, (language, _) in enumerate(subtitle_tracks, start=1):
        command.extend(
            (
                f"-metadata:s:s:{index}",
                f"language={language or 'und'}",
                f"-metadata:s:s:{index}",
                "title=English Subtitles" if language == "eng" else "title=Subtitles",
            )
        )
    command.append(target)
    process = await create_subprocess_exec(
        *command,
        stdout=PIPE,
        stderr=PIPE,
    )
    processes.add(process)
    communicate = create_task(process.communicate())
    cancelled = create_task(cancel_event.wait())
    try:
        done, _ = await wait((communicate, cancelled), return_when=FIRST_COMPLETED)
        if cancelled in done or cancel_event.is_set():
            await _stop_process(process)
            communicate.cancel()
            with suppress(CancelledError):
                await communicate
            raise RuntimeError("Hstream run cancelled")
        _, stderr = await communicate
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="ignore")[-1200:])
    finally:
        processes.discard(process)
        cancelled.cancel()
        with suppress(CancelledError):
            await cancelled
    with suppress(OSError):
        Path(source).unlink()
    with suppress(OSError):
        Path(brand_path).unlink()
    return target


async def _download_quality(
    episode,
    stream,
    subtitle_tracks,
    directory,
    cancel_event,
    processes,
):
    async with _DOWNLOAD_SLOTS:
        if cancel_event.is_set():
            return None
        quality_dir = ospath.join(directory, stream.label)
        Path(quality_dir).mkdir(parents=True, exist_ok=True)
        error = None
        for url in stream.urls:
            try:
                source = await _run_ytdlp(
                    url, quality_dir, episode.source_url, cancel_event, processes
                )
                target = ospath.join(quality_dir, _video_filename(episode, stream))
                return await _remux_to_mkv(
                    source,
                    target,
                    episode,
                    subtitle_tracks,
                    cancel_event,
                    processes,
                )
            except Exception as current:
                error = current
                LOGGER.warning(
                    f"Hstream {episode.title} {stream.label} mirror failed: {current}"
                )
                for item in Path(quality_dir).glob("source.*"):
                    with suppress(OSError):
                        item.unlink()
                if cancel_event.is_set():
                    return None
        LOGGER.error(f"Hstream quality failed: {episode.title} {stream.label}: {error}")
        return None


async def _prepare_episode(resolver, item, index, root, cancel_event, processes, owner_id):
    if cancel_event.is_set():
        return None
    episode = await resolver.resolve(item)
    if not episode.streams:
        LOGGER.warning(f"Hstream has no public streams for {item.url}")
        return None
    directory = ospath.join(root, f"{index:05d}")
    Path(directory).mkdir(parents=True, exist_ok=True)
    cover = await _download_image(
        episode.landscape_url,
        ospath.join(directory, "video_cover.jpg"),
    )
    thumb = ""
    if cover:
        thumb = ospath.join(directory, "video_thumb.jpg")
        await sync_to_async(_copy_thumbnail, cover, thumb)
    subtitle_tracks = await _download_subtitles(episode, directory)
    sample_paths = await _download_sample_images(episode, directory)
    sample_collage = ""
    if sample_paths:
        sample_collage = ospath.join(directory, "sample_collage.jpg")
        sample_collage = await sync_to_async(
            _create_sample_collage,
            sample_paths,
            sample_collage,
        )
    resolution, bit, codec = _quality_summary(episode.streams)
    poster_metadata = {
        "title": episode.title,
        "year": episode.year,
        "description": episode.description,
        "plot": episode.description,
        "synopsis": episode.description,
        "genres": ", ".join(episode.genres) or "N/A",
        "views": f"{episode.views:,}" if episode.views else "N/A",
        "resolution": resolution,
        "bit": bit,
        "codec": codec,
        "category": "anime",
        "landscape_url": episode.landscape_url,
        "portrait_url": episode.portrait_url,
        "poster_url": episode.portrait_url,
        "filename": episode.title,
        "brand": "Anime Starfall",
    }
    owner_settings = user_data.get(owner_id, {})
    template = str(
        owner_settings.get("POST_TEMPLATE_ID") or Config.POST_TEMPLATE_ID or 1
    )
    if template not in {str(value) for value in range(1, POSTER_TEMPLATE_COUNT + 1)}:
        template = "1"
    poster = ""
    try:
        poster = await render_poster_option(
            poster_metadata,
            owner_id,
            user_dict=owner_settings,
            option=template,
        )
        local_poster = ospath.join(directory, "poster.jpg")
        await sync_to_async(Path(poster).replace, local_poster)
        poster = local_poster
    except Exception as error:
        LOGGER.warning(f"Hstream poster generation failed for {episode.title}: {error}")
    results = await gather(
        *(
            _download_quality(
                episode,
                stream,
                subtitle_tracks,
                directory,
                cancel_event,
                processes,
            )
            for stream in episode.streams
        ),
        return_exceptions=True,
    )
    videos = []
    for stream, result in zip(episode.streams, results, strict=True):
        if isinstance(result, Exception):
            LOGGER.error(
                f"Hstream download failed for {episode.title} {stream.label}: {result}"
            )
        elif result:
            videos.append((stream, result))
    if not videos:
        await sync_to_async(rmtree, directory, ignore_errors=True)
        raise RuntimeError(f"No Hstream quality downloaded for {episode.title}")
    if not thumb and videos:
        frame = await get_video_thumbnail(videos[0][1], 0)
        if frame and ospath.isfile(frame):
            cover = ospath.join(directory, "video_cover.jpg")
            thumb = ospath.join(directory, "video_thumb.jpg")
            await sync_to_async(_copy_cover, frame, cover)
            await sync_to_async(_copy_thumbnail, cover, thumb)
            with suppress(OSError):
                Path(frame).unlink()
    return {
        "episode": episode,
        "poster": poster,
        "caption": _poster_caption(episode),
        "thumb": thumb,
        "cover": cover,
        "videos": videos,
        "sample_collage": sample_collage,
        "directory": directory,
    }


def _copy_thumbnail(source, target):
    with Image.open(source) as image:
        image = image.convert("RGB")
        image = ImageOps.fit(image, (320, 180), Image.Resampling.LANCZOS)
        image = image.filter(ImageFilter.UnsharpMask(radius=1, percent=120, threshold=2))
        image.save(target, "JPEG", quality=94, optimize=True, subsampling=0)


def _copy_cover(source, target):
    with Image.open(source) as image:
        image = ImageOps.fit(
            image.convert("RGB"),
            (1280, 720),
            Image.Resampling.LANCZOS,
        )
        image.save(target, "JPEG", quality=94, optimize=True, subsampling=0)


async def _telegram_call(method, **kwargs):
    while True:
        try:
            return await method(**kwargs)
        except FloodWait as error:
            await sleep(max(int(error.value), 1))


async def _upload_episode(prepared, destination, thread_id, cancel_event):
    if not prepared or cancel_event.is_set():
        return 0
    kwargs = {"chat_id": destination}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    if prepared["poster"] and ospath.isfile(prepared["poster"]):
        try:
            await _telegram_call(
                TgClient.bot.send_photo,
                photo=prepared["poster"],
                caption=prepared["caption"],
                **kwargs,
            )
        except Exception as error:
            LOGGER.warning(f"Hstream poster upload failed, sending text: {error}")
            await _telegram_call(
                TgClient.bot.send_message,
                text=prepared["caption"],
                **kwargs,
            )
    else:
        await _telegram_call(
            TgClient.bot.send_message,
            text=prepared["caption"],
            **kwargs,
        )
    uploaded = 0
    for stream, path in prepared["videos"]:
        if cancel_event.is_set():
            break
        info = await _video_info(path)
        height = info["height"] or int(stream.resolution.rstrip("p") or 0)
        width = info["width"] or round(height * 16 / 9)
        media = {
            **kwargs,
            "video": path,
            "caption": f"<b>{escape(ospath.basename(path), quote=False)}</b>",
            "duration": info["duration"],
            "width": width,
            "height": height,
            "supports_streaming": True,
        }
        if prepared["thumb"] and ospath.isfile(prepared["thumb"]):
            media["thumb"] = prepared["thumb"]
        if prepared["cover"] and ospath.isfile(prepared["cover"]):
            media["cover"] = prepared["cover"]
        try:
            await _telegram_call(TgClient.bot.send_video, **media)
        except Exception as error:
            LOGGER.warning(f"Hstream send_video failed, using document: {error}")
            media.pop("video")
            media.pop("supports_streaming", None)
            media.pop("duration", None)
            media.pop("width", None)
            media.pop("height", None)
            media.pop("cover", None)
            media["document"] = path
            await _telegram_call(TgClient.bot.send_document, **media)
        uploaded += 1
    sample_collage = prepared.get("sample_collage")
    if (
        not cancel_event.is_set()
        and sample_collage
        and ospath.isfile(sample_collage)
    ):
        try:
            await _telegram_call(
                TgClient.bot.send_photo,
                photo=sample_collage,
                caption=(
                    "<b>Sample Images</b>\n"
                    f"<b>{escape(prepared['episode'].title, quote=False)}</b>"
                ),
                **kwargs,
            )
        except Exception as error:
            LOGGER.warning(f"Hstream sample collage upload failed: {error}")
    return uploaded


async def hstream_letter_leech(_, message):
    tokens = (message.text or "").split()
    if len(tokens) < 2 or len(tokens[1]) != 1 or not tokens[1].isalnum():
        await send_message(
            message,
            "<b>Usage:</b> <code>/hsll A [-up CHAT_ID|TOPIC_ID]</code>",
        )
        return
    try:
        destination, thread_id = _parse_destination(message, tokens)
    except ValueError as error:
        await send_message(message, str(error))
        return
    if _RUN_LOCK.locked():
        await send_message(message, "Another Hstream letter run is already active.")
        return

    async with _RUN_LOCK:
        controller = BatchTaskController("hsll", message)
        cancel_event = Event()
        processes = set()
        prepare_tasks = set()

        async def cancel_run(_):
            cancel_event.set()
            for task in list(prepare_tasks):
                task.cancel()
            await gather(
                *(_stop_process(process) for process in list(processes)),
                return_exceptions=True,
            )

        controller.register_cancel_callback(cancel_run)
        root = ospath.join(DOWNLOAD_DIR, "hstream", controller.gid)
        Path(root).mkdir(parents=True, exist_ok=True)
        status = None
        uploaded = 0
        failed = 0
        try:
            await TgClient.bot.get_chat(destination)
            async with HstreamResolver() as resolver:
                items = await resolver.discover(tokens[1])
                if not items:
                    await send_message(
                        message,
                        f"No Hstream episodes found for <code>{escape(tokens[1])}</code>.",
                    )
                    return
                cancel_cmd = f"/{BotCommands.CancelTaskCommand[1]}_{controller.gid}"
                status = await send_message(
                    message,
                    (
                        f"<b>Hstream letter {escape(tokens[1].upper())}</b>\n"
                        f"Episodes: <code>{len(items)}</code>\n"
                        "Pipeline: <code>2 downloads / 1 upload</code>\n"
                        f"Stop: <code>{cancel_cmd}</code>"
                    ),
                )

                pending = {}
                next_to_schedule = 0

                def schedule(index):
                    task = create_task(
                        _prepare_episode(
                            resolver,
                            items[index],
                            index,
                            root,
                            cancel_event,
                            processes,
                            controller.user_id,
                        )
                    )
                    pending[index] = task
                    prepare_tasks.add(task)
                    task.add_done_callback(prepare_tasks.discard)

                while next_to_schedule < min(2, len(items)):
                    schedule(next_to_schedule)
                    next_to_schedule += 1

                for index, item in enumerate(items):
                    if cancel_event.is_set():
                        break
                    task = pending.pop(index)
                    try:
                        prepared = await task
                    except CancelledError:
                        if cancel_event.is_set():
                            break
                        raise
                    except Exception as error:
                        LOGGER.error(
                            f"Hstream preparation failed for {item.url}: {error}",
                            exc_info=True,
                        )
                        prepared = None
                        failed += 1
                    if next_to_schedule < len(items) and not cancel_event.is_set():
                        schedule(next_to_schedule)
                        next_to_schedule += 1
                    if prepared:
                        try:
                            uploaded += await _upload_episode(
                                prepared, destination, thread_id, cancel_event
                            )
                        except Exception as error:
                            failed += 1
                            LOGGER.error(
                                f"Hstream upload failed for {item.url}: {error}",
                                exc_info=True,
                            )
                        finally:
                            await sync_to_async(
                                rmtree,
                                prepared["directory"],
                                ignore_errors=True,
                            )
                    if status and not cancel_event.is_set():
                        with suppress(Exception):
                            status = await edit_message(
                                status,
                                (
                                    f"<b>Hstream letter {escape(tokens[1].upper())}</b>\n"
                                    f"Progress: <code>{index + 1}/{len(items)}</code>\n"
                                    f"Uploaded: <code>{uploaded}</code> | Failed: <code>{failed}</code>\n"
                                    f"Stop: <code>{cancel_cmd}</code>"
                                ),
                            )
            if status:
                final = (
                    "Hstream letter run cancelled."
                    if cancel_event.is_set()
                    else "Hstream letter run completed."
                )
                await edit_message(
                    status,
                    f"<b>{final}</b>\nUploaded: <code>{uploaded}</code> | Failed: <code>{failed}</code>",
                )
        except Exception as error:
            LOGGER.error(f"Hstream letter run failed: {error}", exc_info=True)
            await send_message(
                message,
                f"<b>Hstream run failed:</b>\n<code>{escape(str(error)[:900])}</code>",
            )
        finally:
            cancel_event.set()
            for task in list(prepare_tasks):
                task.cancel()
            await gather(*prepare_tasks, return_exceptions=True)
            await gather(
                *(_stop_process(process) for process in list(processes)),
                return_exceptions=True,
            )
            await sync_to_async(rmtree, root, ignore_errors=True)
            controller.close()
