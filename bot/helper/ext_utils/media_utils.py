import re
from contextlib import suppress
from PIL import Image, ImageOps
from hashlib import md5
from aiofiles.os import remove, path as aiopath, makedirs
import json
from asyncio import (
    create_subprocess_exec,
    gather,
    wait_for,
    sleep,
)
from asyncio.subprocess import PIPE
from os import path as ospath
from pathlib import Path
from re import search as re_search, escape
from time import time
from aioshutil import rmtree
from langcodes import Language

from ... import LOGGER, DOWNLOAD_DIR
from ...core.config_manager import BinConfig, Config
from .bot_utils import cmd_exec, sync_to_async
from .ffmpeg_queue import ffmpeg_task
from .files_utils import get_mime_type, is_archive, is_archive_split
from .performance import get_ffmpeg_cores, get_ffmpeg_threads
from .status_utils import get_readable_file_size, get_readable_time, time_to_seconds

_metadata_cache = {}

UPLOADER_TAGS = (
    "Toonworld4all",
    "SubsPlease",
    "EMBER",
    "Erai-raws",
    "HorribleSubs",
    "AnimeRG",
    "Judas",
    "ASW",
    "Anime Time",
    "AnimeShrine",
    "PSA",
)

TITLE_NOISE_PATTERN = (
    r"\b(?:"
    r"19\d{2}|20[0-3]\d|2160p|1080p|720p|480p|4K|DS4K|"
    r"WEB\s?DL|WEB\s?Rip|WEBRIP|Blu\s?Ray|BRRip|BDRip|HDRip|HDTV|DVDRip|REMUX|"
    r"DSNP|AMZN|NF|JHS|Hotstar|HBO|IMAX|CR|MULTI\d*|MULTi\d*|Dual|"
    r"EAC3|E\s?AC3|DDP|AC3|AAC|DTS|TrueHD|Atmos|Opus|FLAC|MP3|"
    r"HEVC|H265|H264|x265|x264|×265|×264|AV1|10bit|8bit|12bit|"
    r"Tamil|Telugu|Hindi|English|Malayalam|Kannada|ESub|MSub|Sub"
    r")\b"
)


def get_md5_hash(up_path):
    md5_hash = md5()
    with open(up_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            md5_hash.update(byte_block)
        return md5_hash.hexdigest()


async def create_thumb(msg, _id=""):
    if not _id:
        _id = time()
        path = f"{DOWNLOAD_DIR}thumbnails"
    else:
        path = "thumbnails"
    await makedirs(path, exist_ok=True)
    photo_dir = await msg.download()
    output = ospath.join(path, f"{_id}.jpg")
    await sync_to_async(Image.open(photo_dir).convert("RGB").save, output, "JPEG")
    await remove(photo_dir)
    return output


async def download_image_thumb(url, landscape=False):
    """Download an image from a URL and save it as a JPEG thumbnail.

    Validates that the URL points to an image via Content-Type header check.
    Returns the path to the saved thumbnail, or empty string on failure.
    """
    from httpx import AsyncClient

    # Content types that are definitely NOT images
    NON_IMAGE_TYPES = (
        "text/", "application/json", "application/xml",
        "application/javascript", "video/", "audio/",
    )
    try:
        async with AsyncClient(verify=False, follow_redirects=True, timeout=30) as client:
            # HEAD request to check content type and size
            try:
                head_resp = await client.head(url)
                content_type = head_resp.headers.get("content-type", "")
                content_length = head_resp.headers.get("content-length", "")
                if content_type and any(
                    content_type.startswith(t) for t in NON_IMAGE_TYPES
                ):
                    LOGGER.error(f"Thumb URL is not an image: {content_type}")
                    return ""

            except Exception:
                pass  # HEAD failed, will check during GET

            # Download the image
            resp = await client.get(url)
            if resp.status_code != 200:
                LOGGER.error(f"Failed to download thumb URL: HTTP {resp.status_code}")
                return ""

            # Only reject known non-image types; unknown types are allowed
            # PIL will validate the actual image data below
            content_type = resp.headers.get("content-type", "")
            if content_type and any(
                content_type.startswith(t) for t in NON_IMAGE_TYPES
            ):
                LOGGER.error(f"Thumb URL is not an image: {content_type}")
                return ""

            data = resp.content

            # Save and convert to JPEG
            path = f"{DOWNLOAD_DIR}thumbnails"
            await makedirs(path, exist_ok=True)
            tmp_path = ospath.join(path, f"{time()}_tmp")
            with open(tmp_path, "wb") as f:
                f.write(data)
            output = ospath.join(path, f"{time()}.jpg")
            def _process_thumb(src, dst):
                with Image.open(src) as im:
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    if landscape:
                        aspect = im.width / max(im.height, 1)
                        if im.width <= im.height or aspect < 1.45 or aspect > 2.35 or im.height < 360:
                            raise ValueError(
                                f"provider image is not a usable landscape thumbnail: {im.width}x{im.height}"
                            )
                        # Preserve provider composition; only scale down oversized images.
                        im.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                    im.save(
                        dst, "JPEG", quality=_thumb_quality(), optimize=True
                    )
                    LOGGER.info(f"Thumbnail source: provider -> {im.size[0]}x{im.size[1]}")
            try:
                await sync_to_async(_process_thumb, tmp_path, output)
            except Exception as e:
                LOGGER.error(f"Failed to process thumb image: {e}")
                with suppress(Exception):
                    await remove(tmp_path)
                return ""
            with suppress(Exception):
                await remove(tmp_path)
            return output
    except Exception as e:
        LOGGER.error(f"Error downloading thumb from URL: {e}")
        return ""


async def get_media_info(path, extra_info=False):
    try:
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
                path,
            ]
        )
    except Exception as e:
        LOGGER.error(f"Get Media Info: {e}. Mostly File not found! - File: {path}")
        return (0, "", "", "") if extra_info else (0, None, None)
    if result[0] and result[2] == 0:
        ffresult = eval(result[0])
        fields = ffresult.get("format")
        if fields is None:
            LOGGER.error(f"get_media_info: {result}")
            return (0, "", "", "") if extra_info else (0, None, None)
        duration = round(float(fields.get("duration", 0)))
        if extra_info:
            lang, qual, stitles = "", "", ""
            if (streams := ffresult.get("streams")) and streams[0].get(
                "codec_type"
            ) == "video":
                qual = int(streams[0].get("height"))
                qual = f"{480 if qual <= 480 else 540 if qual <= 540 else 720 if qual <= 720 else 1080 if qual <= 1080 else 2160 if qual <= 2160 else 4320 if qual <= 4320 else 8640}p"
                for stream in streams:
                    if stream.get("codec_type") == "audio" and (
                        lc := stream.get("tags", {}).get("language")
                    ):
                        with suppress(Exception):
                            lc = Language.get(lc).display_name()
                        if lc not in lang:
                            lang += f"{lc}, "
                    if stream.get("codec_type") == "subtitle" and (
                        st := stream.get("tags", {}).get("language")
                    ):
                        with suppress(Exception):
                            st = Language.get(st).display_name()
                        if st not in stitles:
                            stitles += f"{st}, "
            return duration, qual, lang[:-2], stitles[:-2]
        tags = fields.get("tags", {})
        artist = tags.get("artist") or tags.get("ARTIST") or tags.get("Artist")
        title = tags.get("title") or tags.get("TITLE") or tags.get("Title")
        return duration, artist, title
    return (0, "", "", "") if extra_info else (0, None, None)


async def get_document_type(path):
    is_video, is_audio, is_image = False, False, False
    if (
        is_archive(path)
        or is_archive_split(path)
        or re_search(r".+(\.|_)(rar|7z|zip|bin)(\.0*\d+)?$", path)
    ):
        return is_video, is_audio, is_image
    mime_type = await sync_to_async(get_mime_type, path)
    if mime_type.startswith("image"):
        return False, False, True
    try:
        result = await cmd_exec(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                path,
            ]
        )
        if result[1] and mime_type.startswith("video"):
            is_video = True
    except Exception as e:
        LOGGER.error(f"Get Document Type: {e}. Mostly File not found! - File: {path}")
        if mime_type.startswith("audio"):
            return False, True, False
        if not mime_type.startswith("video") and not mime_type.endswith("octet-stream"):
            return is_video, is_audio, is_image
        if mime_type.startswith("video"):
            is_video = True
        return is_video, is_audio, is_image
    if result[0] and result[2] == 0:
        fields = eval(result[0]).get("streams")
        if fields is None:
            LOGGER.error(f"get_document_type: {result}")
            return is_video, is_audio, is_image
        is_video = False
        for stream in fields:
            if stream.get("codec_type") == "video":
                codec_name = stream.get("codec_name", "").lower()
                if codec_name not in {"mjpeg", "png", "bmp"}:
                    is_video = True
            elif stream.get("codec_type") == "audio":
                is_audio = True
    return is_video, is_audio, is_image


async def get_streams(file):
    """
    Gets media stream information using ffprobe.

    Args:
        file: Path to the media file.

    Returns:
        A list of stream objects (dictionaries) or None if an error occurs
        or no streams are found.
    """
    cmd = [
        "ffprobe",
        "-hide_banner",
        "-loglevel",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        file,
    ]
    process = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        LOGGER.error(f"Error getting stream info: {stderr.decode().strip()}")
        return None

    try:
        return json.loads(stdout)["streams"]
    except KeyError:
        LOGGER.error(
            f"No streams found in the ffprobe output: {stdout.decode().strip()}",
        )
        return None


async def take_ss(video_file, ss_nb) -> bool:
    duration = (await get_media_info(video_file))[0]
    if duration != 0:
        dirpath, name = video_file.rsplit("/", 1)
        name, _ = ospath.splitext(name)
        dirpath = f"{dirpath}/{name}_mltbss"
        await makedirs(dirpath, exist_ok=True)
        interval = duration // (ss_nb + 1)
        cap_time = interval
        cmds = []
        for i in range(ss_nb):
            output = f"{dirpath}/SS.{name}_{i:02}.png"
            cmd = [
                "taskset",
                "-c",
                get_ffmpeg_cores(),
                BinConfig.FFMPEG_NAME,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{cap_time}",
                "-i",
                video_file,
                "-q:v",
                "1",
                "-frames:v",
                "1",
                "-threads",
                str(get_ffmpeg_threads()),
                output,
            ]
            cap_time += interval
            cmds.append(cmd)
        try:
            async with ffmpeg_task(label="Screenshot generation"):
                for cmd in cmds:
                    result = await wait_for(cmd_exec(cmd), timeout=60)
                    if result[2] != 0:
                        LOGGER.error(
                            f"Error while creating screenshots from video. Path: {video_file}. stderr: {result[1]}"
                        )
                        await rmtree(dirpath, ignore_errors=True)
                        return False
        except Exception:
            LOGGER.error(
                f"Error while creating screenshots from video. Path: {video_file}. Error: Timeout some issues with ffmpeg with specific arch!"
            )
            await rmtree(dirpath, ignore_errors=True)
            return False
        return dirpath
    else:
        LOGGER.error("take_ss: Can't get the duration of video")
        return False


async def get_audio_thumbnail(audio_file):
    output_dir = f"{DOWNLOAD_DIR}thumbnails"
    await makedirs(output_dir, exist_ok=True)
    output = ospath.join(output_dir, f"{time()}.jpg")
    cmd = [
        "taskset",
        "-c",
        get_ffmpeg_cores(),
        BinConfig.FFMPEG_NAME,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        audio_file,
        "-an",
        "-vcodec",
        "copy",
        "-threads",
        str(get_ffmpeg_threads()),
        output,
    ]
    try:
        async with ffmpeg_task(label="Audio thumbnail"):
            _, err, code = await wait_for(cmd_exec(cmd), timeout=60)
        if code != 0 or not await aiopath.exists(output):
            LOGGER.error(
                f"Error while extracting thumbnail from audio. Name: {audio_file} stderr: {err}"
            )
            return None
    except Exception:
        LOGGER.error(
            f"Error while extracting thumbnail from audio. Name: {audio_file}. Error: Timeout some issues with ffmpeg with specific arch!"
        )
        return None
    return output


async def get_video_thumbnail(video_file, duration):
    output_dir = f"{DOWNLOAD_DIR}thumbnails"
    await makedirs(output_dir, exist_ok=True)
    output = ospath.join(output_dir, f"{time()}.jpg")
    if duration is None:
        duration = (await get_media_info(video_file))[0]
    if duration == 0:
        duration = 3
    duration = max(1, duration // 10)
    cmd = [
        "taskset",
        "-c",
        get_ffmpeg_cores(),
        BinConfig.FFMPEG_NAME,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{duration}",
        "-i",
        video_file,
        "-vf",
        "thumbnail,scale='min(1280,iw)':-2",
        "-q:v",
        "1",
        "-frames:v",
        "1",
        "-threads",
        str(get_ffmpeg_threads()),
        output,
    ]
    try:
        async with ffmpeg_task(label="Video thumbnail"):
            _, err, code = await wait_for(cmd_exec(cmd), timeout=60)
        if code != 0 or not await aiopath.exists(output):
            LOGGER.error(
                f"Error while extracting thumbnail from video. Name: {video_file} stderr: {err}"
            )
            return None
        def _optimize_thumb(path):
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.save(
                    path, "JPEG", quality=_thumb_quality(), optimize=True
                )
                LOGGER.info(f"Thumbnail source: FFmpeg frame -> {im.size[0]}x{im.size[1]}")
        await sync_to_async(_optimize_thumb, output)
    except Exception:
        LOGGER.error(
            f"Error while extracting thumbnail from video. Name: {video_file}. Error: Timeout some issues with ffmpeg with specific arch!"
        )
        return None
    return output


async def get_telegram_document_thumb(thumb_path):
    """Create a Telegram-safe document thumbnail from an existing HD thumbnail."""
    if not thumb_path or thumb_path == "none" or not await aiopath.exists(thumb_path):
        return thumb_path
    output_dir = f"{DOWNLOAD_DIR}thumbnails"
    await makedirs(output_dir, exist_ok=True)
    output = ospath.join(output_dir, f"{time()}_doc.jpg")

    def _make_doc_thumb(src, dst):
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((320, 320), Image.Resampling.LANCZOS)
            im.save(dst, "JPEG", quality=90, optimize=True)

    try:
        await sync_to_async(_make_doc_thumb, thumb_path, output)
        return output
    except Exception as e:
        LOGGER.warning(f"Document thumb generation failed: {e}")
        with suppress(Exception):
            await remove(output)
        return thumb_path


async def get_multiple_frames_thumbnail(video_file, layout, keep_screenshots):
    layout = re.sub(r"(\d+)\D+(\d+)", r"\1x\2", layout)
    ss_nb = layout.split("x")
    if len(ss_nb) != 2 or not ss_nb[0].isdigit() or not ss_nb[1].isdigit():
        LOGGER.error(f"Invalid layout value: {layout}")
        return None
    ss_nb = int(ss_nb[0]) * int(ss_nb[1])
    if ss_nb == 0:
        LOGGER.error(f"Invalid layout value: {layout}")
        return None
    dirpath = await take_ss(video_file, ss_nb)
    if not dirpath:
        return None
    output_dir = f"{DOWNLOAD_DIR}thumbnails"
    await makedirs(output_dir, exist_ok=True)
    output = ospath.join(output_dir, f"{time()}.jpg")
    cmd = [
        "taskset",
        "-c",
        get_ffmpeg_cores(),
        BinConfig.FFMPEG_NAME,
        "-hide_banner",
        "-loglevel",
        "error",
        "-pattern_type",
        "glob",
        "-i",
        f"{escape(dirpath)}/*.png",
        "-vf",
        f"tile={layout}, thumbnail",
        "-q:v",
        "1",
        "-frames:v",
        "1",
        "-f",
        "mjpeg",
        "-threads",
        str(get_ffmpeg_threads()),
        output,
    ]
    try:
        async with ffmpeg_task(label="Contact sheet"):
            _, err, code = await wait_for(cmd_exec(cmd), timeout=60)
        if code != 0 or not await aiopath.exists(output):
            LOGGER.error(
                f"Error while combining thumbnails for video. Name: {video_file} stderr: {err}"
            )
            return None
    except Exception:
        LOGGER.error(
            f"Error while combining thumbnails from video. Name: {video_file}. Error: Timeout some issues with ffmpeg with specific arch!"
        )
        return None
    finally:
        if not keep_screenshots:
            await rmtree(dirpath, ignore_errors=True)
    return output


class FFMpeg:
    def __init__(self, listener):
        self._listener = listener
        self._processed_bytes = 0
        self._last_processed_bytes = 0
        self._processed_time = 0
        self._last_processed_time = 0
        self._speed_raw = 0
        self._speed_text = ""
        self._progress_raw = 0
        self._total_time = 0
        self._eta_raw = 0
        self._time_rate = 0.1
        self._start_time = 0

    @property
    def processed_bytes(self):
        return self._processed_bytes

    @property
    def speed_raw(self):
        return self._speed_raw

    @property
    def progress_raw(self):
        return self._progress_raw

    @property
    def eta_raw(self):
        return self._eta_raw

    def clear(self):
        self._start_time = time()
        self._processed_bytes = 0
        self._processed_time = 0
        self._speed_raw = 0
        self._speed_text = ""
        self._progress_raw = 0
        self._eta_raw = 0
        self._time_rate = 0.1
        self._last_processed_time = 0
        self._last_processed_bytes = 0

    async def _ffmpeg_progress(self):
        while not (
            self._listener.subproc.returncode is not None
            or self._listener.is_cancelled
            or self._listener.subproc.stdout.at_eof()
        ):
            try:
                line = await wait_for(self._listener.subproc.stdout.readline(), 60)
            except Exception:
                break
            line = line.decode().strip()
            if not line:
                break
            if "=" in line:
                key, value = line.split("=", 1)
                if value != "N/A":
                    if key == "total_size":
                        self._processed_bytes = int(value) + self._last_processed_bytes
                        self._speed_raw = self._processed_bytes / (
                            time() - self._start_time
                        )
                    elif key == "speed":
                        self._speed_text = value
                        self._time_rate = max(0.1, float(value.strip("x")))
                    elif key == "out_time":
                        self._processed_time = (
                            time_to_seconds(value) + self._last_processed_time
                        )
                        try:
                            self._progress_raw = (
                                self._processed_time * 100
                            ) / self._total_time
                            if (
                                hasattr(self._listener, "subsize")
                                and self._listener.subsize
                                and self._progress_raw > 0
                            ):
                                self._processed_bytes = int(
                                    self._listener.subsize * (self._progress_raw / 100)
                                )
                            if (time() - self._start_time) > 0:
                                self._speed_raw = self._processed_bytes / (
                                    time() - self._start_time
                                )
                            else:
                                self._speed_raw = 0
                            self._eta_raw = (
                                self._total_time - self._processed_time
                            ) / self._time_rate
                        except ZeroDivisionError:
                            self._progress_raw = 0
                            self._eta_raw = 0
            await sleep(0.05)

    async def ffmpeg_cmds(self, ffmpeg, f_path):
        self.clear()
        self._total_time = (await get_media_info(f_path))[0]
        base_name, ext = ospath.splitext(f_path)
        dir, base_name = base_name.rsplit("/", 1)
        indices = [
            index
            for index, item in enumerate(ffmpeg)
            if item.startswith("mltb") or item == "mltb"
        ]
        outputs = []
        for index in indices:
            output_file = ffmpeg[index]
            if output_file != "mltb" and output_file.startswith("mltb"):
                bo, oext = ospath.splitext(output_file)
                if oext:
                    if ext == oext:
                        prefix = f"ffmpeg{index}." if bo == "mltb" else ""
                    else:
                        prefix = ""
                    ext = ""
                else:
                    prefix = ""
            else:
                prefix = f"ffmpeg{index}."
            output = f"{dir}/{prefix}{output_file.replace('mltb', base_name)}{ext}"
            outputs.append(output)
            ffmpeg[index] = output
        if self._listener.is_cancelled:
            return False
        async with ffmpeg_task(self._listener, "Custom FFmpeg"):
            self._listener.subproc = await create_subprocess_exec(
                *ffmpeg, stdout=PIPE, stderr=PIPE
            )
            await self._ffmpeg_progress()
            _, stderr = await self._listener.subproc.communicate()
        code = self._listener.subproc.returncode
        if self._listener.is_cancelled:
            return False
        if code == 0:
            return outputs
        elif code == -9:
            self._listener.is_cancelled = True
            return False
        else:
            try:
                stderr = stderr.decode().strip()
            except Exception:
                stderr = "Unable to decode the error!"
            LOGGER.error(
                f"{stderr}. Something went wrong while running ffmpeg cmd, mostly file requires different/specific arguments. Path: {f_path}"
            )
            for op in outputs:
                if await aiopath.exists(op):
                    await remove(op)
            return False

    async def convert_video(self, video_file, ext, retry=False):
        self.clear()
        self._total_time = (await get_media_info(video_file))[0]
        base_name = ospath.splitext(video_file)[0]
        output = f"{base_name}.{ext}"
        if retry:
            cmd = [
                "taskset",
                "-c",
                get_ffmpeg_cores(),
                BinConfig.FFMPEG_NAME,
                "-hide_banner",
                "-loglevel",
                "error",
                "-progress",
                "pipe:1",
                "-i",
                video_file,
                "-map",
                "0",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-threads",
                str(get_ffmpeg_threads()),
                output,
            ]
            if ext == "mp4":
                cmd[17:17] = ["-c:s", "mov_text"]
            elif ext == "mkv":
                cmd[17:17] = ["-c:s", "ass"]
            else:
                cmd[17:17] = ["-c:s", "copy"]
        else:
            cmd = [
                "taskset",
                "-c",
                get_ffmpeg_cores(),
                BinConfig.FFMPEG_NAME,
                "-hide_banner",
                "-loglevel",
                "error",
                "-progress",
                "pipe:1",
                "-i",
                video_file,
                "-map",
                "0",
                "-c",
                "copy",
                "-threads",
                str(get_ffmpeg_threads()),
                output,
            ]
        if self._listener.is_cancelled:
            return False
        async with ffmpeg_task(self._listener, "Video convert"):
            self._listener.subproc = await create_subprocess_exec(
                *cmd, stdout=PIPE, stderr=PIPE
            )
            await self._ffmpeg_progress()
            _, stderr = await self._listener.subproc.communicate()
        code = self._listener.subproc.returncode
        if self._listener.is_cancelled:
            return False
        if code == 0:
            return output
        elif code == -9:
            self._listener.is_cancelled = True
            return False
        else:
            if await aiopath.exists(output):
                await remove(output)
            if not retry:
                return await self.convert_video(video_file, ext, True)
            try:
                stderr = stderr.decode().strip()
            except Exception:
                stderr = "Unable to decode the error!"
            LOGGER.error(
                f"{stderr}. Something went wrong while converting video, mostly file need specific codec. Path: {video_file}"
            )
        return False

    async def convert_audio(self, audio_file, ext):
        self.clear()
        self._total_time = (await get_media_info(audio_file))[0]
        base_name = ospath.splitext(audio_file)[0]
        output = f"{base_name}.{ext}"
        cmd = [
            "taskset",
            "-c",
            get_ffmpeg_cores(),
            BinConfig.FFMPEG_NAME,
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            audio_file,
            "-threads",
            str(get_ffmpeg_threads()),
            output,
        ]
        if self._listener.is_cancelled:
            return False
        async with ffmpeg_task(self._listener, "Audio convert"):
            self._listener.subproc = await create_subprocess_exec(
                *cmd, stdout=PIPE, stderr=PIPE
            )
            await self._ffmpeg_progress()
            _, stderr = await self._listener.subproc.communicate()
        code = self._listener.subproc.returncode
        if self._listener.is_cancelled:
            return False
        if code == 0:
            return output
        elif code == -9:
            self._listener.is_cancelled = True
            return False
        else:
            try:
                stderr = stderr.decode().strip()
            except Exception:
                stderr = "Unable to decode the error!"
            LOGGER.error(
                f"{stderr}. Something went wrong while converting audio, mostly file need specific codec. Path: {audio_file}"
            )
            if await aiopath.exists(output):
                await remove(output)
        return False

    async def sample_video(self, video_file, sample_duration, part_duration):
        self.clear()
        self._total_time = sample_duration
        dir, name = video_file.rsplit("/", 1)
        output_file = f"{dir}/SAMPLE.{name}"
        segments = [(0, part_duration)]
        duration = (await get_media_info(video_file))[0]
        remaining_duration = duration - (part_duration * 2)
        parts = (sample_duration - (part_duration * 2)) // part_duration
        time_interval = remaining_duration // parts
        next_segment = time_interval
        for _ in range(parts):
            segments.append((next_segment, next_segment + part_duration))
            next_segment += time_interval
        segments.append((duration - part_duration, duration))

        filter_complex = ""
        for i, (start, end) in enumerate(segments):
            filter_complex += (
                f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}]; "
            )
            filter_complex += (
                f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]; "
            )

        for i in range(len(segments)):
            filter_complex += f"[v{i}][a{i}]"

        filter_complex += f"concat=n={len(segments)}:v=1:a=1[vout][aout]"

        cmd = [
            "taskset",
            "-c",
            get_ffmpeg_cores(),
            BinConfig.FFMPEG_NAME,
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            video_file,
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-threads",
            str(get_ffmpeg_threads()),
            output_file,
        ]

        if self._listener.is_cancelled:
            return False
        async with ffmpeg_task(self._listener, "Sample video"):
            self._listener.subproc = await create_subprocess_exec(
                *cmd, stdout=PIPE, stderr=PIPE
            )
            await self._ffmpeg_progress()
            _, stderr = await self._listener.subproc.communicate()
        code = self._listener.subproc.returncode
        if self._listener.is_cancelled:
            return False
        if code == -9:
            self._listener.is_cancelled = True
            return False
        elif code == 0:
            return output_file
        else:
            try:
                stderr = stderr.decode().strip()
            except Exception:
                stderr = "Unable to decode the error!"
            LOGGER.error(
                f"{stderr}. Something went wrong while creating sample video, mostly file is corrupted. Path: {video_file}"
            )
            if await aiopath.exists(output_file):
                await remove(output_file)
            return False

    async def split(self, f_path, file_, parts, split_size):
        self.clear()
        multi_streams = True
        self._total_time = duration = (await get_media_info(f_path))[0]
        base_name, extension = ospath.splitext(file_)
        split_size -= 3000000
        start_time = 0
        i = 1
        while i <= parts or start_time < duration - 4:
            out_path = f_path.replace(file_, f"{base_name}.part{i:02}{extension}")
            cmd = [
                "taskset",
                "-c",
                get_ffmpeg_cores(),
                BinConfig.FFMPEG_NAME,
                "-hide_banner",
                "-loglevel",
                "error",
                "-progress",
                "pipe:1",
                "-ss",
                str(start_time),
                "-i",
                f_path,
                "-fs",
                str(split_size),
                "-map",
                "0",
                "-map_chapters",
                "-1",
                "-async",
                "1",
                "-strict",
                "-2",
                "-c",
                "copy",
                "-threads",
                str(get_ffmpeg_threads()),
                out_path,
            ]
            if not multi_streams:
                del cmd[15]
                del cmd[15]
            if self._listener.is_cancelled:
                return False
            async with ffmpeg_task(self._listener, "Video split"):
                self._listener.subproc = await create_subprocess_exec(
                    *cmd, stdout=PIPE, stderr=PIPE
                )
                await self._ffmpeg_progress()
                _, stderr = await self._listener.subproc.communicate()
            code = self._listener.subproc.returncode
            if self._listener.is_cancelled:
                return False
            if code == -9:
                self._listener.is_cancelled = True
                return False
            elif code != 0:
                try:
                    stderr = stderr.decode().strip()
                except Exception:
                    stderr = "Unable to decode the error!"
                with suppress(Exception):
                    await remove(out_path)
                if multi_streams:
                    LOGGER.warning(
                        f"{stderr}. Retrying without map, -map 0 not working in all situations. Path: {f_path}"
                    )
                    multi_streams = False
                    continue
                else:
                    LOGGER.warning(
                        f"{stderr}. Unable to split this video, if it's size less than {self._listener.max_split_size} will be uploaded as it is. Path: {f_path}"
                    )
                return False
            out_size = await aiopath.getsize(out_path)
            if out_size > self._listener.max_split_size:
                split_size -= (out_size - self._listener.max_split_size) + 5000000
                LOGGER.warning(
                    f"Part size is {out_size}. Trying again with lower split size!. Path: {f_path}"
                )
                await remove(out_path)
                continue
            lpd = (await get_media_info(out_path))[0]
            if lpd == 0:
                LOGGER.error(
                    f"Something went wrong while splitting, mostly file is corrupted. Path: {f_path}"
                )
                break
            elif duration == lpd:
                LOGGER.warning(
                    f"This file has been splitted with default stream and audio, so you will only see one part with less size from orginal one because it doesn't have all streams and audios. This happens mostly with MKV videos. Path: {f_path}"
                )
                break
            elif lpd <= 3:
                await remove(out_path)
                break
            self._last_processed_time += lpd
            self._last_processed_bytes += out_size
            start_time += lpd - 3
            i += 1
        return True


def _clean_rename_token(value):
    value = str(value or "").replace("_", " ").replace(".", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -._")


def clean_rss_filename(filename):
    stem, ext = ospath.splitext(str(filename or ""))
    placeholders = {}

    def hold(match):
        key = f"RSSDATETOKEN{len(placeholders)}RSS"
        placeholders[key] = match.group(0)
        return key

    stem = re.sub(r"(?<!\d)(?:\d{2}|\d{4})[._-]\d{2}[._-]\d{2}(?!\d)", hold, stem)
    stem = re.sub(r"[._]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    for key, value in placeholders.items():
        stem = stem.replace(key, value.replace("_", ".").replace("-", "."))
    return f"{stem}{ext}" if stem else filename


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return ""


def _thumb_quality():
    try:
        quality = int(Config.AUTO_THUMBNAIL_QUALITY)
    except (TypeError, ValueError):
        quality = 95
    return max(1, min(quality, 100))


def _extract_bit_tag(*values):
    source = " ".join(str(value or "") for value in values)
    bit_match = re.search(
        r"(?<!\d)(8|10|12)\s*[-_.]?\s*bit\b|\bhi\s*(8|10|12)\s*p\b|\bmain\s*(10|12)\b",
        source,
        re.IGNORECASE,
    )
    if not bit_match:
        return ""
    bit_value = next((group for group in bit_match.groups() if group), "")
    return "" if bit_value == "8" else f"{bit_value}bit"


def _sanitize_filename(name, fallback):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(name or ""))
    name = re.sub(r"\s+", " ", name).strip(" .-_")
    if not name:
        return fallback

    ext = Path(name).suffix or Path(fallback).suffix
    stem = name[: -len(ext)] if ext and name.lower().endswith(ext.lower()) else name
    stem = stem.strip(" .-_") or Path(fallback).stem
    max_stem_len = max(1, 255 - len(ext))
    return f"{stem[:max_stem_len].strip(' .-_')}{ext}"


def _normalize_resolution(value):
    if not value:
        return ""
    value = value.lower()
    if value == "4k":
        return "2160p"
    return value.replace("p", "") + "p" if value.endswith("p") else value


def _extract_source_quality(filename):
    quality_patterns = [
        (r"\bWEB[-\s.]?DL\b", "WEB-DL"),
        (r"\bWEB[-\s.]?Rip\b", "WEBRip"),
        (r"\bBlu[-\s.]?Ray\b", "BluRay"),
        (r"\bBD[-\s.]?Rip\b", "BDRip"),
        (r"\bBR[-\s.]?Rip\b", "BRRip"),
        (r"\bHDRip\b", "HDRip"),
        (r"\bHDTV\b", "HDTV"),
        (r"\bDVDRip\b", "DVDRip"),
        (r"\bCAMRip\b|\bCAM\b", "CAMRip"),
        (r"\bTeleSync\b|\bTS\b", "TS"),
        (r"\bRemux\b", "REMUX"),
    ]
    for pattern, label in quality_patterns:
        if re.search(pattern, filename, re.IGNORECASE):
            return label
    return ""


def _extract_dynamic_range(filename):
    patterns = [
        (r"\bDV\b|\bDolby[\s._-]?Vision\b", "DV"),
        (r"\bHDR10\+\b", "HDR10+"),
        (r"\bHDR10\b", "HDR10"),
        (r"\bHDR\b", "HDR"),
        (r"\bSDR\b", "SDR"),
    ]
    for pattern, label in patterns:
        if re.search(pattern, filename, re.IGNORECASE):
            return label
    return ""


def _extract_codec_tag(filename):
    patterns = [
        (r"\b(?:HEVC|H\.?265|x265|h265)\b", "×265"),
        (r"\b(?:AVC|H\.?264|x264|h264)\b", "×264"),
        (r"\bAV1\b", "AV1"),
    ]
    for pattern, label in patterns:
        if re.search(pattern, filename, re.IGNORECASE):
            return label
    return ""


def _extract_ott_tag(filename):
    ott_patterns = [
        (r"\bDSNP\b|\bDisney(?:\+| Plus)?\b", "DSNP"),
        (r"\bJHS\b|\bJioHotstar\b", "JHS"),
        (r"\bHS\b|\bHotstar\b", "HS"),
        (r"\bAMZN\b|\bAmazon\b|\bPrime\b", "AMZN"),
        (r"\bNF\b|\bNetflix\b", "NF"),
        (r"\bIMAX\b", "IMAX"),
        (r"\bHBO\b|\bMAX\b", "HBO"),
        (r"\bCR\b|\bCrunchyroll\b", "CR"),
        (r"\bZEE5\b", "ZEE5"),
        (r"\bSonyLIV\b|\bSLIV\b", "SonyLIV"),
        (r"\bAHA\b", "AHA"),
        (r"\bSUNNXT\b", "SUNNXT"),
        (r"\bJioCinema\b|\bJIO\b", "JioCinema"),
    ]
    for pattern, label in ott_patterns:
        if re.search(pattern, filename, re.IGNORECASE):
            return label
    return ""


def _has_ds4k(filename):
    return bool(re.search(r"\bDS4K\b", str(filename or ""), re.IGNORECASE))


def _extract_release_group(filename):
    stem = Path(filename).stem
    release_match = re.search(
        r"(?:^|\s)-\s*([A-Za-z0-9][A-Za-z0-9._-]{1,30})(?:[\s\]\)]|$)",
        stem,
    )
    if release_match:
        return release_match.group(1).strip(" -._")

    bracket_tags = re.findall(r"\[([A-Za-z0-9][A-Za-z0-9 ._-]{1,30})\]", stem)
    skip_words = {
        "tamil",
        "hindi",
        "english",
        "esub",
        "multi",
        "web-dl",
        "webrip",
        "bluray",
        "1080p",
        "720p",
        "2160p",
    }
    for tag in reversed(bracket_tags):
        clean = _clean_rename_token(tag)
        if clean and clean.lower() not in skip_words:
            return clean
    return ""


def _normalize_channel_tag(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"\s+", "", text)
    text = text.replace("_", ".").replace("-", ".")
    text = re.sub(r"\.+", ".", text).strip(".")
    if text in {"2", "2ch", "2.0", "20"}:
        return "2.0"
    if text in {"6", "6ch", "5.1", "51"}:
        return "5.1"
    if text in {"8", "8ch", "7.1", "71"}:
        return "7.1"
    match = re.match(r"^([257])\.?([01])", text)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return text.upper()


def _extract_audio_tag(filename):
    channel_pattern = (
        r"2[\s._-]*(?:\.|_)?[\s._-]*0|"
        r"5[\s._-]*(?:\.|_)?[\s._-]*1|"
        r"7[\s._-]*(?:\.|_)?[\s._-]*1|"
        r"[268][\s._-]*CH"
    )
    match = re.search(
        rf"\b(E[-\s.]?AC[-\s.]?3|DDP|DD\+|AAC|DTS[-\s.]?HD|DTS|TrueHD|Atmos|Opus|FLAC|MP3)(?:[\s._-]*({channel_pattern}))?",
        filename,
        re.IGNORECASE,
    )
    if not match:
        return ""
    codec = match.group(1).replace(" ", "").replace(".", "").replace("-", "").upper()
    channels = _normalize_channel_tag(match.group(2))
    return f"{codec} {channels}".strip()


def _normalize_audio_codec(codec):
    codec = str(codec or "").lower()
    if codec in {"eac3", "e-ac-3", "ddp", "dd+"}:
        return "DDP"
    if codec == "ac3":
        return "AC3"
    if codec == "aac":
        return "AAC"
    if "dts" in codec:
        return "DTS"
    if "truehd" in codec:
        return "TrueHD"
    if codec == "opus":
        return "Opus"
    if codec == "flac":
        return "FLAC"
    if codec == "mp3":
        return "MP3"
    return codec.upper() if codec else ""


def _normalize_video_codec(codec):
    codec = str(codec or "").lower()
    if codec in {"hevc", "h265", "x265"}:
        return "×265"
    if codec in {"h264", "avc", "x264"}:
        return "×264"
    if codec == "av1":
        return "AV1"
    return codec.upper() if codec else ""


def _normalize_channels_count(channels):
    try:
        channels = int(channels)
    except (TypeError, ValueError):
        return ""
    return {
        1: "1.0",
        2: "2.0",
        6: "5.1",
        8: "7.1",
    }.get(channels, f"{channels}ch")


def _extract_part_tag(filename):
    match = re.search(r"\b(?:part|pt|p)[\s._-]*0*(\d{1,3})\b", filename, re.IGNORECASE)
    return f"P{int(match.group(1)):02}" if match else ""


def _short_language_tag(languages):
    if not languages:
        return ""
    parts = [part.strip() for part in re.split(r"[,/|]+", languages) if part.strip()]
    if len(parts) >= 3:
        return f"Multi{len(parts)}"
    if len(parts) == 2:
        return "Dual"
    return parts[0]


def _short_subtitle_tag(subtitles, filename):
    text = f"{subtitles} {filename}".lower()
    if "msub" in text or len([p for p in re.split(r"[,/|]+", subtitles or "") if p.strip()]) > 1:
        return "MSub"
    if "esub" in text or "english" in text or re.search(r"\beng?\b", text):
        return "ESub"
    return "Sub" if subtitles else ""


def _pretty_bitrate(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if any(unit in text.lower() for unit in ("kb/s", "mb/s", "kbps", "mbps")):
        return text.replace(" ", "")
    try:
        value = int(float(text))
    except (TypeError, ValueError):
        return text
    return f"{round(value / 1000)}kbps" if value else ""


async def _extract_mediainfo_rename_info(filepath):
    info = {}
    try:
        stdout, _, code = await cmd_exec(["mediainfo", "--Output=JSON", filepath])
        if code != 0 or not stdout:
            return info
        data = json.loads(stdout)
        tracks = (data.get("media") or {}).get("track") or []
    except Exception as e:
        LOGGER.warning(f"MediaInfo rename metadata failed: {e}")
        return info

    audio_codecs = []
    audio_channels = []
    audio_bitrates = []
    audio_parts = []
    atmos = False
    for track in tracks:
        if track.get("@type") != "Audio":
            continue
        fmt = (
            track.get("Format_Commercial_IfAny")
            or track.get("Format")
            or track.get("CodecID")
            or ""
        )
        title = f"{track.get('Title', '')} {track.get('CommercialName', '')}".lower()
        acodec = _normalize_audio_codec(fmt)
        channels = (
            track.get("Channels/String")
            or _normalize_channels_count(track.get("Channels"))
            or track.get("ChannelLayout")
        )
        channels = _normalize_channel_tag(
            str(channels or "")
            .replace(" channels", "")
            .replace("channel(s)", "")
            .replace(" ", "")
        )
        if channels.isdigit():
            channels = _normalize_channels_count(channels)
        bitrate = _pretty_bitrate(
            track.get("BitRate")
            or track.get("BitRate/String")
            or track.get("BitRate_Nominal")
        )
        if "atmos" in title or "joc" in title:
            atmos = True
        if acodec and acodec not in audio_codecs:
            audio_codecs.append(acodec)
        if channels and channels not in audio_channels:
            audio_channels.append(channels)
        if bitrate and bitrate not in audio_bitrates:
            audio_bitrates.append(bitrate)
        part = " ".join(p for p in (acodec, channels, "Atmos" if atmos else "") if p)
        if part and part not in audio_parts:
            audio_parts.append(part)
    if audio_codecs:
        info["acodec"] = audio_codecs[0]
        info["audio_codec"] = "/".join(audio_codecs)
    if audio_channels:
        info["audio_channels"] = "/".join(audio_channels)
    if audio_bitrates:
        info["audio_bitrate"] = "/".join(audio_bitrates)
    if audio_parts:
        info["audio"] = audio_parts[0]
    return info


async def _enrich_template_metadata(metadata, filename, filepath=None, extra=None):
    metadata = {key: str(value or "") for key, value in metadata.items()}
    extra = extra or {}
    for key, value in extra.items():
        metadata[key] = str(value or "")

    raw_name = Path(filename).stem
    extension = Path(filename).suffix
    metadata.setdefault("title", metadata.get("name", ""))
    metadata["file_name"] = filename
    metadata["raw_name"] = raw_name
    metadata["extension"] = extension
    metadata["name"] = metadata.get("title", "")
    metadata["file_caption"] = metadata.get("file_caption") or metadata.get("precaption", "")
    metadata["link"] = metadata.get("link", "")
    metadata["part"] = metadata.get("part") or _extract_part_tag(filename)
    metadata["audio"] = metadata.get("audio") or _extract_audio_tag(filename)
    date_match = re.search(
        r"(?<!\d)((?:\d{2}|\d{4})[._-]\d{2}[._-]\d{2})(?!\d)",
        filename,
    )
    if date_match and not metadata.get("date"):
        metadata["date"] = date_match.group(1).replace("_", ".").replace("-", ".")
    if not metadata.get("episode_name"):
        stem_after_date = raw_name
        if date_match:
            stem_after_date = raw_name[date_match.end():]
        stem_after_date = re.split(
            r"(?i)(?:\b(?:xxx|porn|jav|1080p|720p|480p|2160p|4k|hevc|x265|x264|web[-_. ]?dl|bluray)\b)",
            stem_after_date.replace(".", " ").replace("_", " "),
            maxsplit=1,
        )[0]
        metadata["episode_name"] = re.sub(r"\s+", " ", stem_after_date).strip(" -._")

    if filepath and await aiopath.exists(filepath):
        try:
            metadata["file_size"] = get_readable_file_size(await aiopath.getsize(filepath))
        except Exception:
            metadata.setdefault("file_size", "")
        try:
            duration, resolution, languages, subtitles = await get_media_info(filepath, True)
            metadata.setdefault("duration", get_readable_time(duration) if duration else "")
            if resolution and not metadata.get("resolution"):
                metadata["resolution"] = resolution
            if languages:
                metadata["languages"] = languages
            if subtitles:
                metadata["subtitles"] = subtitles
        except Exception as e:
            LOGGER.warning(f"Template media metadata failed for {filename}: {e}")
    metadata.setdefault("file_size", metadata.get("size", ""))
    metadata["size"] = metadata.get("size") or metadata.get("file_size", "")
    if not metadata.get("languages") and metadata.get("language"):
        metadata["languages"] = metadata.get("language", "")
    if not metadata.get("language") and metadata.get("languages"):
        metadata["language"] = metadata.get("languages", "")
    metadata["shortlang"] = metadata.get("shortlang") or _short_language_tag(metadata.get("languages", ""))
    metadata["shortsub"] = metadata.get("shortsub") or _short_subtitle_tag(
        metadata.get("subtitles", ""), filename
    )
    metadata["DS4K"] = metadata.get("DS4K") or ("DS4K" if _has_ds4k(filename) else "")
    for key in (
        "file_name", "file_size", "file_caption", "languages", "language", "subtitles",
        "duration", "ott", "source", "resolution", "name", "title", "year", "quality",
        "season", "episode", "audio", "lib", "extension", "shortsub",
        "shortlang", "part", "raw_name", "link", "vcodec", "codec", "acodec",
        "audio_codec", "audio_channels", "audio_bitrate", "hdr",
        "dynamic_range", "release_group", "group", "DS4K", "bit", "size",
        "date", "episode_name", "episodes", "genres", "rating", "plot", "synopsis",
        "start", "end", "range", "range_tag", "filename", "upload_filename",
    ):
        metadata.setdefault(key, "")
    return metadata


def _clean_title_from_filename(filename):
    stem = Path(filename).stem
    uploader_pattern = "|".join(re.escape(tag) for tag in UPLOADER_TAGS)
    stem = re.sub(
        rf"^\[(?:{uploader_pattern})\]\s*",
        "",
        stem,
        flags=re.IGNORECASE,
    )
    stem = re.sub(r"^[^\w\[\(]{1,12}\s*[-_. ]+", "", stem, flags=re.UNICODE)
    stem = re.sub(r"^[A-Z0-9]{1,8}\s*[-_. ]+(?=\[?[Ss]\d{1,2}[Ee]\d{1,4}\]?)", "", stem)
    stem = re.sub(r"(?:^|\s)-\s*[A-Za-z0-9][A-Za-z0-9._-]{1,30}$", "", stem)
    title = re.sub(r"[\[\](){}]", " ", stem)
    title = title.replace(".", " ").replace("_", " ").replace("-", " ")
    title = re.sub(r"\s+", " ", title).strip()

    title = re.sub(
        r"^(?:\s*S0*\d{1,2}\s*(?:-?\s*(?:E|EP)\s*\(?\s*0*\d{1,4}"
        r"\s+(?:0*\d{1,4})\s*\)?)\s*)+",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()

    merge_range = re.search(
        r"^\s*[Ss]0*\d{1,2}\s*EP\s*\d{1,4}\s+\d{1,4}\s+",
        title,
        re.IGNORECASE,
    )
    if merge_range:
        title = title[merge_range.end():].strip()

    sxe = re.search(
        r"(?<![A-Za-z0-9])[Ss]0*(\d{1,2})[\s._-]*[Ee]0*(\d{1,4})(?![A-Za-z0-9])",
        title,
    )
    if sxe:
        title = title[: sxe.start()] if sxe.start() > 0 else title[sxe.end():]

    parts = re.split(
        TITLE_NOISE_PATTERN,
        title,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    title = parts[0]
    if not title.strip() and len(parts) > 1:
        title = re.sub(
            rf"^(?:\s*{TITLE_NOISE_PATTERN}\s*)+",
            "",
            " ".join(parts[1:]),
            flags=re.IGNORECASE,
        )
    title = re.sub(r"\s+", " ", title).strip(" -._")
    return title


async def _extract_stream_rename_info(filepath):
    info = {}
    if not filepath or not await aiopath.exists(filepath):
        return info
    info.update(await _extract_mediainfo_rename_info(filepath))
    try:
        streams = await get_streams(filepath)
    except Exception as e:
        LOGGER.warning(f"AutoRename stream metadata failed: {e}")
        return info
    if not streams:
        return info
    audio_parts = []
    audio_codecs = []
    audio_channels = []
    audio_bitrates = []
    atmos = False
    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and not info.get("vcodec"):
            height = stream.get("height")
            if height:
                try:
                    height = int(height)
                    if height <= 480:
                        info["resolution"] = "480p"
                    elif height <= 720:
                        info["resolution"] = "720p"
                    elif height <= 1080:
                        info["resolution"] = "1080p"
                    elif height <= 2160:
                        info["resolution"] = "2160p"
                    else:
                        info["resolution"] = f"{height}p"
                except (TypeError, ValueError):
                    pass
            vcodec = _normalize_video_codec(stream.get("codec_name"))
            if vcodec:
                info["vcodec"] = vcodec
                info["codec"] = vcodec
            bit_depth = (
                stream.get("bits_per_raw_sample")
                or stream.get("bits_per_sample")
                or ""
            )
            pix_fmt = str(stream.get("pix_fmt") or "")
            profile = str(stream.get("profile") or "")
            bit_tag = _extract_bit_tag(bit_depth, pix_fmt, profile)
            if bit_tag and not info.get("bit"):
                info["bit"] = bit_tag
            dyn_source = " ".join(
                str(stream.get(k, ""))
                for k in ("color_transfer", "color_primaries", "pix_fmt", "profile")
            ).lower()
            if "dovi" in dyn_source or "dolby" in dyn_source:
                info["hdr"] = "DV"
                info["dynamic_range"] = "DV"
            elif "smpte2084" in dyn_source or "hdr" in dyn_source:
                info["hdr"] = "HDR"
                info["dynamic_range"] = "HDR"
            elif "bt709" in dyn_source or pix_fmt:
                info.setdefault("dynamic_range", "SDR")
        elif codec_type == "audio":
            acodec = _normalize_audio_codec(stream.get("codec_name"))
            channels = _normalize_channels_count(stream.get("channels"))
            bitrate = stream.get("bit_rate") or ""
            tags = stream.get("tags") or {}
            title = f"{tags.get('title', '')} {stream.get('profile', '')}".lower()
            if "atmos" in title:
                atmos = True
            if acodec and acodec not in audio_codecs:
                audio_codecs.append(acodec)
            if channels and channels not in audio_channels:
                audio_channels.append(channels)
            if bitrate:
                try:
                    br = int(bitrate)
                    pretty = f"{round(br / 1000)}kbps"
                    if pretty not in audio_bitrates:
                        audio_bitrates.append(pretty)
                except (TypeError, ValueError):
                    pass
            part = " ".join(p for p in (acodec, channels, "Atmos" if atmos else "") if p)
            if part and part not in audio_parts:
                audio_parts.append(part)
    if audio_codecs and not info.get("acodec"):
        info["acodec"] = audio_codecs[0]
    if audio_codecs and not info.get("audio_codec"):
        info["audio_codec"] = "/".join(audio_codecs)
    if audio_channels and not info.get("audio_channels"):
        info["audio_channels"] = "/".join(audio_channels)
    if audio_bitrates and not info.get("audio_bitrate"):
        info["audio_bitrate"] = "/".join(audio_bitrates)
    if audio_parts and not info.get("audio"):
        info["audio"] = audio_parts[0]
    return info


async def build_caption_metadata(filename, filepath=None, **extra):
    supplied_metadata = extra.pop("template_metadata", None)
    source_filename = str(extra.get("source_filename") or filename or "")
    merge_metadata = (
        _extract_merge_range_metadata(source_filename)
        or _extract_merge_range_metadata(filename)
    )
    metadata_seed = choose_media_title_seed(filename, **extra)
    metadata = (
        dict(supplied_metadata)
        if supplied_metadata
        else await extract_metadata_from_filename(metadata_seed, filepath)
    )
    if merge_metadata:
        metadata.update(merge_metadata)
    metadata = {key: str(value or "") for key, value in metadata.items()}
    metadata.setdefault("filename", filename)
    metadata["filename"] = filename
    metadata.setdefault("upload_filename", filename)

    if filepath and await aiopath.exists(filepath):
        try:
            metadata["size"] = get_readable_file_size(await aiopath.getsize(filepath))
        except Exception:
            metadata["size"] = ""
        try:
            duration, resolution, languages, subtitles = await get_media_info(
                filepath, True
            )
            metadata["duration"] = get_readable_time(duration) if duration else ""
            if resolution and not metadata.get("resolution"):
                metadata["resolution"] = resolution
            metadata["languages"] = languages or ""
            metadata["subtitles"] = subtitles or ""
        except Exception as e:
            LOGGER.warning(f"Caption media metadata failed for {filename}: {e}")
        try:
            metadata["md5_hash"] = await sync_to_async(get_md5_hash, filepath)
        except Exception:
            metadata["md5_hash"] = ""
    else:
        metadata.update(
            {
                "size": "",
                "duration": "",
                "languages": "",
                "subtitles": "",
                "md5_hash": "",
            }
        )

    metadata = await _enrich_template_metadata(metadata, filename, filepath, extra)
    if merge_metadata:
        metadata.update(merge_metadata)
    if not metadata.get("languages") and metadata.get("language"):
        metadata["languages"] = metadata.get("language", "")
    if not metadata.get("language") and metadata.get("languages"):
        metadata["language"] = metadata.get("languages", "")
    return _SafeFormatDict(metadata)


def _extract_merge_range_metadata(filename):
    merge_range = re.search(
        r"(?:\[S0*(\d{1,2})-)?EP\(\s*(\d{1,4})\s*-\s*(\d{1,4})\s*\)\]?",
        str(filename or ""),
        re.IGNORECASE,
    )
    if not merge_range:
        return {}
    start = merge_range.group(2).zfill(2)
    end = merge_range.group(3).zfill(2)
    episode = f"{start}-{end}"
    season = (merge_range.group(1) or "1").lstrip("0") or "1"
    return {
        "season": season,
        "start": start,
        "end": end,
        "episode": episode,
        "episodes": episode,
        "range": f"EP({episode})",
        "range_tag": f"[S{season}-EP({episode})]",
    }


def apply_caption_word_replace(text, rules):
    """Apply sequential per-user caption replacement/removal rules."""
    result = str(text or "")
    if not rules:
        return result
    for rule in str(rules).split("|"):
        rule = rule.strip()
        if not rule:
            continue
        old, separator, replacement = rule.partition(":")
        old = old.strip()
        if not old:
            continue
        result = result.replace(old, replacement.strip() if separator else "")
    return result


async def _resolve_imdb_title(title, year=None):
    title = _clean_rename_token(title)
    if not title or title.lower() == "unknown":
        return title
    try:
        from imdbinfo import get_movie, search_title

        def lookup():
            results = search_title(title).titles
            if not results:
                return ""
            if year:
                results = [
                    item for item in results
                    if str(getattr(item, "year", "") or "") == str(year)
                ] or results
            results = [
                item for item in results
                if getattr(item, "kind", "") in (
                    "movie",
                    "tvSeries",
                    "tvMiniSeries",
                    "tvEpisode",
                    "video",
                )
            ] or results
            movie = get_movie(results[0].id)
            return getattr(movie, "title", "") or getattr(results[0], "title", "")

        resolved = await sync_to_async(lookup)
        return resolved or title
    except Exception as e:
        LOGGER.warning(f"IMDb title lookup failed for '{title}': {e}")
        return title


def _looks_like_anime_name(filename, title):
    anime_tokens = (
        "anime",
        "subsplease",
        "erai-raws",
        "horriblesubs",
        "anime time",
        "judas",
        "ember",
        "animeshrine",
        "toonworld4all",
        "animepahe",
        "hianime",
        "aniwatch",
        "crunchyroll",
        "b-global",
    )
    text = f"{filename} {title}".lower()
    if any(token in text for token in anime_tokens):
        return True
    title_words = set(re.findall(r"[a-z0-9]+", str(title).lower()))
    romanized_particles = {
        "chan",
        "de",
        "ga",
        "kara",
        "kun",
        "made",
        "na",
        "ni",
        "no",
        "san",
        "sensei",
        "senpai",
        "to",
        "wa",
        "wo",
    }
    episode_hint = re.search(
        r"(?i)(?:\b(?:ep|episode|e)\s*0*\d{1,4}\b|"
        r"\s-\s*0*\d{1,4}(?=[\s\._-]|$)|"
        r"[\s._-]0*\d{1,4}(?=[\s._-]+(?:2160p|1080p|720p|480p|4k)(?:\b|[._-])))",
        str(filename),
    )
    return bool(episode_hint and title_words & romanized_particles)


async def _resolve_tmdb_title(title, year=None):
    title = _clean_rename_token(title)
    if not title or not Config.TMDB_ACCESS_TOKEN:
        return ""
    try:
        from httpx import AsyncClient

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
        async with AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.themoviedb.org/3/search/multi",
                params=params,
                headers=headers,
            )
        if resp.status_code != 200:
            LOGGER.warning(f"TMDb title lookup failed with status {resp.status_code}")
            return ""
        results = [
            item for item in resp.json().get("results", [])
            if item.get("media_type") in {"movie", "tv"}
        ]
        if not results:
            return ""
        if year:
            wanted_year = str(year)
            result = next(
                (
                    item
                    for item in results
                    if str(
                        item.get("release_date")
                        or item.get("first_air_date")
                        or ""
                    ).startswith(wanted_year)
                ),
                results[0],
            )
        else:
            result = results[0]
        return result.get("title") or result.get("name") or ""
    except Exception as e:
        LOGGER.warning(f"TMDb title lookup failed for '{title}': {e}")
        return ""


async def _fetch_anilist_media(title):
    title = _clean_rename_token(title)
    if not title:
        return {}
    cache_key = f"anilist:{title.lower()}"
    if cache_key in _metadata_cache:
        return _metadata_cache[cache_key]
    query = """
    query ($search: String!) {
      Page(page: 1, perPage: 5) {
        media(search: $search, type: ANIME) {
          id
          title { english romaji native }
          bannerImage
          coverImage { extraLarge large }
          description(asHtml: false)
          siteUrl
          genres
          seasonYear
        }
      }
    }
    """
    try:
        from httpx import AsyncClient, TimeoutException

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 StarFallX/1.2",
        }
        payload = {"query": query, "variables": {"search": title}}
        for attempt in range(2):
            try:
                async with AsyncClient(timeout=12, headers=headers) as client:
                    resp = await client.post(
                        "https://graphql.anilist.co",
                        json=payload,
                    )
                if resp.status_code != 200:
                    LOGGER.warning(
                        f"AniList title lookup failed with status {resp.status_code}"
                    )
                    if resp.status_code >= 500:
                        await sleep(1)
                        continue
                    return {}
                results = (
                    resp.json()
                    .get("data", {})
                    .get("Page", {})
                    .get("media")
                    or []
                )
                media = next((item for item in results if item.get("bannerImage")), None)
                media = media or (results[0] if results else {})
                if media:
                    _metadata_cache[cache_key] = media
                return media
            except TimeoutException:
                LOGGER.warning(
                    f"AniList title lookup timed out (attempt {attempt + 1}/2)"
                )
                await sleep(1)
        return {}
    except Exception as e:
        LOGGER.warning(f"AniList title lookup failed for '{title}': {e}")
        return {}


async def _resolve_anilist_title(title):
    media = await _fetch_anilist_media(title)
    titles = media.get("title") or {}
    return titles.get("english") or titles.get("romaji") or titles.get("native") or ""


async def get_anilist_poster_link(title, as_doc=False):
    media = await _fetch_anilist_media(title)
    if not media:
        return None
    cover = media.get("coverImage") or {}
    if as_doc:
        return cover.get("extraLarge") or cover.get("large") or media.get("bannerImage")
    # Video thumbnails must remain landscape. A portrait cover is useful for
    # documents/posters, but Telegram crops it poorly as a video thumbnail.
    return media.get("bannerImage")


async def _fetch_mal_media(title):
    title = _clean_rename_token(title)
    client_id = getattr(Config, "MYANIMELIST_CLIENT_ID", "")
    if not title or not client_id:
        return {}
    cache_key = f"mal:{title.lower()}"
    if cache_key in _metadata_cache:
        return _metadata_cache[cache_key]
    try:
        from httpx import AsyncClient, TimeoutException

        headers = {
            "X-MAL-CLIENT-ID": client_id,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 StarFallX/1.2",
        }
        params = {
            "q": title,
            "limit": 5,
            "fields": "id,title,alternative_titles,main_picture,start_date,synopsis",
        }
        for attempt in range(2):
            try:
                async with AsyncClient(timeout=12, headers=headers) as client:
                    resp = await client.get(
                        "https://api.myanimelist.net/v2/anime",
                        params=params,
                    )
                if resp.status_code != 200:
                    LOGGER.warning(
                        f"MyAnimeList lookup failed with status {resp.status_code}"
                    )
                    if resp.status_code >= 500:
                        await sleep(1)
                        continue
                    return {}
                items = resp.json().get("data") or []
                media = (items[0] or {}).get("node") if items else {}
                if media:
                    _metadata_cache[cache_key] = media
                return media or {}
            except TimeoutException:
                LOGGER.warning(
                    f"MyAnimeList lookup timed out (attempt {attempt + 1}/2)"
                )
                await sleep(1)
        return {}
    except Exception as e:
        LOGGER.warning(f"MyAnimeList lookup failed for '{title}': {e}")
        return {}


async def _resolve_mal_title(title):
    media = await _fetch_mal_media(title)
    return media.get("title") or ""


async def get_mal_poster_link(title, as_doc=False):
    media = await _fetch_mal_media(title)
    picture = media.get("main_picture") or {}
    return picture.get("large") or picture.get("medium")


async def _fetch_jikan_media(title):
    title = _clean_rename_token(title)
    if not title:
        return {}
    cache_key = f"jikan:{title.lower()}"
    if cache_key in _metadata_cache:
        return _metadata_cache[cache_key]
    try:
        from httpx import AsyncClient

        async with AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.jikan.moe/v4/anime",
                params={"q": title, "limit": 1},
            )
        if resp.status_code != 200:
            LOGGER.warning(f"Jikan lookup failed with status {resp.status_code}")
            return {}
        data = (resp.json().get("data") or [{}])[0] or {}
        if data:
            _metadata_cache[cache_key] = data
        return data
    except Exception as e:
        LOGGER.warning(f"Jikan lookup failed for '{title}': {e}")
        return {}


async def _fetch_kitsu_media(title):
    title = _clean_rename_token(title)
    if not title:
        return {}
    cache_key = f"kitsu:{title.lower()}"
    if cache_key in _metadata_cache:
        return _metadata_cache[cache_key]
    try:
        from httpx import AsyncClient

        async with AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://kitsu.io/api/edge/anime",
                params={"filter[text]": title, "page[limit]": 1},
            )
        if resp.status_code != 200:
            LOGGER.warning(f"Kitsu lookup failed with status {resp.status_code}")
            return {}
        data = ((resp.json().get("data") or [{}])[0] or {}).get("attributes") or {}
        if data:
            _metadata_cache[cache_key] = data
        return data
    except Exception as e:
        LOGGER.warning(f"Kitsu lookup failed for '{title}': {e}")
        return {}


async def _resolve_jikan_title(title):
    media = await _fetch_jikan_media(title)
    return media.get("title_english") or media.get("title") or ""


async def _resolve_kitsu_title(title):
    media = await _fetch_kitsu_media(title)
    titles = media.get("titles") or {}
    return titles.get("en") or titles.get("en_jp") or titles.get("ja_jp") or media.get("canonicalTitle") or ""


async def get_jikan_poster_link(title, as_doc=False):
    media = await _fetch_jikan_media(title)
    images = ((media.get("images") or {}).get("jpg") or {})
    return images.get("large_image_url") or images.get("image_url")


async def get_kitsu_poster_link(title, as_doc=False):
    media = await _fetch_kitsu_media(title)
    if as_doc:
        poster = media.get("posterImage") or {}
        return poster.get("original") or poster.get("large")
    cover = media.get("coverImage") or {}
    poster = media.get("posterImage") or {}
    return cover.get("original") or cover.get("large") or poster.get("original") or poster.get("large")


async def get_release_description(title):
    media = await _fetch_anilist_media(title)
    if media.get("description"):
        return re.sub(r"<[^>]+>", "", media["description"]).strip()
    media = await _fetch_jikan_media(title)
    if media.get("synopsis"):
        return media["synopsis"].strip()
    media = await _fetch_kitsu_media(title)
    return str(media.get("synopsis") or "").strip()


async def _resolve_media_title(title, filename, year=None):
    title = _clean_rename_token(title)
    if not title or title.lower() == "unknown":
        return title
    if _looks_like_anime_name(filename, title):
        anilist_title = await _resolve_anilist_title(title)
        if anilist_title:
            return anilist_title
        mal_title = await _resolve_mal_title(title)
        if mal_title:
            return mal_title
        return title
    tmdb_title = await _resolve_tmdb_title(title, year)
    if tmdb_title:
        return tmdb_title
    anilist_title = await _resolve_anilist_title(title)
    if anilist_title:
        return anilist_title
    mal_title = await _resolve_mal_title(title)
    if mal_title:
        return mal_title
    return title


async def extract_metadata_from_filename(filename, filepath=None):
    """Extract title, season, episode, source, and media fields from a filename.

    Ported from WZMLakane leech_utils.py with extended patterns for anime,
    TV shows, movies, and manga chapter naming conventions.
    """
    metadata = {
        "title": "Unknown",
        "season": "1",
        "episode": "01",
        "resolution": "",
        "bit": "",
        "ott": "",
        "quality": "",
        "lib": "",
        "year": "",
        "chapter": "001",
        "vcodec": "",
        "codec": "",
        "acodec": "",
        "audio_codec": "",
        "audio_channels": "",
        "audio_bitrate": "",
        "hdr": "",
        "dynamic_range": "",
        "release_group": "",
        "group": "",
        "DS4K": "",
        "start": "",
        "end": "",
        "range": "",
        "date": "",
        "episode_name": "",
    }

    pattern = (
        r"^\[(?:" + "|".join(re.escape(tag) for tag in UPLOADER_TAGS) + r")\]\s*"
    )
    clean_filename = re.sub(pattern, "", filename, flags=re.IGNORECASE).strip()

    merge_range = re.search(
        r"^\[S0*(\d{1,2})-EP\(\s*(\d{1,4})\s*-\s*(\d{1,4})\s*\)\]",
        clean_filename,
        re.IGNORECASE,
    )
    if merge_range:
        metadata["season"] = merge_range.group(1)
        metadata["start"] = merge_range.group(2).zfill(2)
        metadata["end"] = merge_range.group(3).zfill(2)
        metadata["episode"] = f"{metadata['start']}-{metadata['end']}"
        metadata["range"] = f"EP({metadata['episode']})"

    date_match = re.search(
        r"(?<!\d)((?:\d{2}|\d{4})[._-]\d{2}[._-]\d{2})(?!\d)",
        clean_filename,
    )
    if date_match:
        metadata["date"] = date_match.group(1).replace("_", ".").replace("-", ".")

    title_patterns = [
        r"^(.+?)[\s\.\-]*(?<![A-Za-z0-9])[Ss]0*(\d{1,2})[\s\.\-]*[Ee]0*(\d{1,4})(?![A-Za-z0-9])",
        r"^(.+?)[\s\.\-]*[Ss]eason[\s\.\-]*0*(\d+)[\s\.\-]*[Ee]pisode[\s\.\-]*0*(\d+)",
        r"^\[CH[-\s]?\d+\][\s\.\-]*(.+?)[\s\.\-]*-",
        r"^\[\d+\][\s\.\-]*(.+?)[\s\.\-]*(?:@|$)",
        r"^(.+?)[\s\.\-]*(?:Ch(?:apter)?|#)[\s\.\-]*\d+",
        r"^(.+?)[\s\.\-]*\[",
        r"^0*(\d{1,4})[\s\.\-_]+(.+?)(?:[\s\.\-_]+|@|$)",
    ]

    title_found = False
    episode_found = bool(merge_range)

    for pat in title_patterns:
        title_match = re.search(pat, clean_filename, re.IGNORECASE)
        if title_match:
            if len(title_match.groups()) >= 3:
                title = (
                    title_match.group(1).replace(".", " ").replace("-", " ").strip()
                )
                metadata["season"] = title_match.group(2)
                ep_num = int(title_match.group(3))
                metadata["episode"] = str(ep_num).zfill(
                    4 if ep_num >= 1000 else 3 if ep_num >= 100 else 2
                )
                episode_found = True
            elif (
                len(title_match.groups()) == 2
                and pat
                == r"^0*(\d{1,4})[\s\.\-_]+(.+?)(?:[\s\.\-_]+|@|$)"
            ):
                ep_num = int(title_match.group(1))
                if 1 <= ep_num <= 9999 and ep_num < 1920:
                    metadata["episode"] = str(ep_num).zfill(
                        4 if ep_num >= 1000 else 3 if ep_num >= 100 else 2
                    )
                    episode_found = True
                title = (
                    title_match.group(2)
                    .replace(".", " ")
                    .replace("-", " ")
                    .replace("_", " ")
                    .strip()
                )
            else:
                title = (
                    title_match.group(1).replace(".", " ").replace("-", " ").strip()
                )
            title = re.sub(
                r"\s*[\(\[]?\s*(199[0-9]|20[0-2][0-9]|2030)\s*[\)\]]?\s*",
                " ",
                title,
            ).strip()
            clean_title = _clean_title_from_filename(title)
            if clean_title:
                title = clean_title
            metadata["title"] = title
            title_found = True
            break

    if not title_found and clean_filename:
        base_title = _clean_title_from_filename(clean_filename)
        if base_title:
            base_title = re.sub(
                r"\s*[\(\[]?\s*(199[0-9]|20[0-2][0-9]|2030)\s*[\)\]]?\s*",
                " ",
                base_title,
            ).strip()
            metadata["title"] = base_title

    season_match = re.search(
        r"(?<![A-Za-z0-9])(?:[Ss]eason[\s\.\-]*|[Ss])0*(\d{1,2})(?![A-Za-z0-9])",
        filename,
    )
    if season_match:
        metadata["season"] = season_match.group(1)

    if metadata.get("date") and not metadata.get("episode_name"):
        after_date = clean_filename[date_match.end():] if date_match else ""
        after_date = re.split(
            r"(?i)(?:\b(?:xxx|porn|jav|1080p|720p|480p|2160p|4k|hevc|x265|x264|web[-_. ]?dl|bluray)\b)",
            after_date.replace(".", " ").replace("_", " "),
            maxsplit=1,
        )[0]
        metadata["episode_name"] = re.sub(r"\s+", " ", after_date).strip(" -._")

    if not metadata.get("episode_name"):
        ep_name_match = re.search(
            r"(?i)(?:S\d{1,2}\s*[._ -]*E\d{1,4}|Episode\s*[._ -]*\d{1,4}|Ep\s*[._ -]*\d{1,4}|E\d{1,4})[._ -]+(.+)",
            clean_filename,
        )
        if ep_name_match:
            ep_name = ep_name_match.group(1).replace("_", " ").replace(".", " ")
            ep_name = re.split(
                r"(?i)(?:\b(?:2160p|1080p|720p|480p|4k|WEB[-_. ]?DL|WEB[-_. ]?Rip|BluRay|HDRip|H\.?264|H\.?265|x264|x265|HEVC|AV1|AAC|DDP|EAC3|AC3|Multi|Dual|ESub|MSub)\b|\[[A-Fa-f0-9]{6,}\])",
                ep_name,
                maxsplit=1,
            )[0]
            ep_name = re.sub(r"[-\s]+", " ", ep_name).strip(" -._")
            if ep_name and not re.fullmatch(r"[A-Fa-f0-9]{6,}", ep_name):
                metadata["episode_name"] = ep_name

    if not episode_found:
        episode_patterns = [
            r"^0*(\d{1,4})[\s\.\-_]+(?![xX]\d)",
            r"(?<![A-Za-z0-9])(?:[Ee]pisode|[Ee]p|[Ee])[\s\.\-]*0*(\d{1,4})(?![A-Za-z0-9])",
            r"[\s\.\-]+-[\s\.\-]*0*(\d+)(?=[\s\.\-]|\.mkv|\.mp4|\.avi|$)",
            r"[\s\.\-]+0*(\d+)[\s\.\-]+\[",
            r"[\s\.\-]+-[\s\.\-]+0*(\d+)[\s\.\-]+\[",
            r"[\s\.\-]+0*(\d+)[\s\.\-]+\d{3,4}p",
            r"[\s\.\-]+0*(\d+)[\s\.\-]*\[(?!CH)",
            r"\[0*(\d+)\](?!p)",
            r"[\s\._\-]0*(\d+)(?=[\s\._\-](?:END|Final|Fin|v\d|BD|WEB|BluRay))",
            r"[\-][\s]*0*(\d{1,4})(?=[\s\.\-]|$)",
            r"[_]0*(\d{1,4})(?=[\s\._\-]|$)",
            r"[\s\.\-]x0*(\d+)(?=[\s\.\-]|$)",
            r"[\s\.\-]~[\s]*0*(\d+)(?=[\s\.\-]|$)",
            r"#0*(\d+)(?=[\s\.\-]|$)",
            r"[\s\.\-]0*(\d+)(?:st|nd|rd|th)[\s\.\-]",
            r"[Pp]art[\s\.\-]*0*(\d+)(?=[\s\.\-]|$)",
        ]

        for pat in episode_patterns:
            episode_match = re.search(pat, filename, re.IGNORECASE)
            if episode_match:
                ep_value = episode_match.group(1)
                try:
                    ep_num = int(ep_value)
                    if pat == r"^0*(\d{1,4})[\s\.\-_]+(?![xX]\d)":
                        if ep_num >= 1920 or ep_num == 0:
                            continue
                    if 1 <= ep_num <= 9999:
                        if ep_num > 999:
                            context_check = re.search(
                                rf"(?:episode|ep|e)-?\s*0*{ep_value}",
                                filename,
                                re.IGNORECASE,
                            )
                            if not context_check:
                                continue
                        metadata["episode"] = str(ep_num).zfill(
                            4 if ep_num >= 1000 else 3 if ep_num >= 100 else 2
                        )
                        episode_found = True
                        break
                except ValueError:
                    continue

        if not episode_found:
            title_part = re.split(r"[\.\-\s]+\d{3,4}p", filename)[0]
            title_part = re.sub(r"\[.*?\]|\(.*?\)", "", title_part).strip()
            fallback_match = re.search(
                r"[\s\.\-]+0*(\d{1,4})(?=[\s\.\-]|$)", title_part
            )
            if fallback_match:
                ep_num = int(fallback_match.group(1))
                if 1 <= ep_num <= 9999:
                    metadata["episode"] = str(ep_num).zfill(
                        4 if ep_num >= 1000 else 3 if ep_num >= 100 else 2
                    )

    resolution_match = re.search(r"(\d{3,4}p|4K|2160p)", filename, re.IGNORECASE)
    if resolution_match:
        metadata["resolution"] = _normalize_resolution(resolution_match.group(1))

    chapter_patterns = [
        r"\[CH[-\s]?(\d+)\]",
        r"\[(?:CH|Ch|ch)[-\s]?(\d+)\]",
        r"\[(\d{2,4})\]",
        r"Ch(?:apter)?[-_\s]?(\d+)",
        r"\b[Cc][-\s]?(\d+)\b",
        r"#(\d+)",
        r"\bEp(?:isode)?[-_\s]?(\d+)\b",
        r"\bVol(?:ume)?[-_\s]?(\d+)\b",
        r"\bPart[-_\s]?(\d+)\b",
        r"\bChap[-_\s]?(\d+)\b",
        r"\bBook[-_\s]?(\d+)\b",
        r"\bE(\d{2,4})\b",
            r"(?<![A-Za-z0-9])S\d+E(\d+)(?![A-Za-z0-9])",
        r"\[(?:C|c)(\d+)\]",
    ]

    for pat in chapter_patterns:
        chapter_match = re.search(pat, filename, re.IGNORECASE)
        if chapter_match:
            metadata["chapter"] = chapter_match.group(1).zfill(3)
            break

    year_match = re.search(r"\b(19\d{2}|20[0-3]\d)\b", filename)
    if year_match:
        metadata["year"] = year_match.group(1)

    source_quality = _extract_source_quality(filename)
    if source_quality:
        metadata["quality"] = source_quality
    if _has_ds4k(filename):
        metadata["DS4K"] = "DS4K"

    bit_tag = _extract_bit_tag(filename)
    if bit_tag:
        metadata["bit"] = bit_tag

    metadata["ott"] = _extract_ott_tag(filename)
    metadata["lib"] = _extract_release_group(filename)
    metadata["release_group"] = metadata["lib"]
    metadata["group"] = metadata["lib"]
    metadata["codec"] = _extract_codec_tag(filename)
    metadata["vcodec"] = metadata["codec"]
    metadata["hdr"] = _extract_dynamic_range(filename)
    metadata["dynamic_range"] = metadata["hdr"]
    audio_tag = _extract_audio_tag(filename)
    if audio_tag:
        parts = audio_tag.split()
        metadata["audio"] = audio_tag
        metadata["audio_codec"] = parts[0]
        metadata["acodec"] = parts[0]
        if len(parts) > 1:
            metadata["audio_channels"] = parts[1]

    stream_info = await _extract_stream_rename_info(filepath)
    for key, value in stream_info.items():
        if value and not metadata.get(key):
            metadata[key] = value
    if stream_info.get("resolution") and not resolution_match:
        metadata["resolution"] = stream_info["resolution"]
    if stream_info.get("bit") and not metadata["bit"]:
        metadata["bit"] = stream_info["bit"]
    if not metadata["title"] or metadata["title"].lower() == "unknown":
        metadata["title"] = _clean_title_from_filename(filename)

    metadata["title"] = await _resolve_media_title(
        metadata["title"],
        filename,
        metadata.get("year") or None,
    )
    if not metadata["title"] or metadata["title"].lower() == "unknown":
        metadata["title"] = _clean_title_from_filename(filename)
    if metadata.get("lib") and metadata["title"].lower() == metadata["lib"].lower():
        metadata["lib"] = ""
    if not metadata.get("release_group"):
        metadata["release_group"] = metadata.get("lib", "")
    if not metadata.get("group"):
        metadata["group"] = metadata.get("release_group", "")

    return metadata


async def apply_template_rename(filename, template, filepath=None, **extra):
    """Apply a template-based rename using metadata extracted from the filename.

    Supports math offset tags like {episode:+12} or {season:-1}.
    Returns the renamed filename, preserving the original extension.
    """
    if not template or "{" not in template:
        return filename
    supplied_metadata = extra.pop("template_metadata", None)
    source_filename = str(extra.get("source_filename") or filename or "")
    merge_metadata = (
        _extract_merge_range_metadata(source_filename)
        or _extract_merge_range_metadata(filename)
    )
    metadata_seed = choose_media_title_seed(filename, **extra)
    metadata = (
        dict(supplied_metadata)
        if supplied_metadata
        else await extract_metadata_from_filename(metadata_seed, filepath)
    )
    if merge_metadata:
        metadata.update(merge_metadata)
    metadata = await _enrich_template_metadata(metadata, filename, filepath, extra)
    if merge_metadata:
        metadata.update(merge_metadata)

    def _apply_math_offset(tmpl, meta):
        def replacer(m):
            tag = m.group(1)
            sign = m.group(2)
            offset = int(m.group(3))
            raw = meta.get(tag, "")
            if not raw:
                return m.group(0)
            try:
                original_num = int(raw)
                pad_width = len(raw)
                offset_val = offset if sign == "+" else -offset
                result = original_num + offset_val
                if result <= 0:
                    result = original_num
                new_str = str(result).zfill(pad_width)
                meta[tag] = new_str
                return f"{{{tag}}}"
            except ValueError:
                return m.group(0)

        patched = re.sub(r"\{(episode|season):([+\-])(\d+)\}", replacer, tmpl)
        return patched

    template = _apply_math_offset(template, metadata)

    try:
        renamed = template.format_map(_SafeFormatDict(metadata))
        original_ext = Path(filename).suffix
        if original_ext and not renamed.lower().endswith(original_ext.lower()):
            renamed += original_ext
        renamed = _sanitize_filename(renamed, filename)
        # Guard: if rename produced empty or whitespace-only filename, keep original
        if not renamed.strip() or renamed.strip() == original_ext:
            return filename
        return renamed
    except (KeyError, ValueError, IndexError):
        return filename


def apply_regex_rename(filename, pattern_str):
    """Apply regex-based rename using pipe-separated pattern:replacement pairs.

    Format: |pattern1:replacement1|pattern2:replacement2
    Returns the renamed filename.
    """
    if not pattern_str:
        return filename
    parts = pattern_str.strip().split("|")
    result = filename
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            pat, repl = part.split(":", 1)
        else:
            pat = part
            repl = ""
        try:
            result = re.sub(pat, repl, result)
        except re.error:
            continue
    # Guard: if regex produced empty filename, keep original
    if not result.strip():
        return filename
    return result


def _final_clean(title):
    """Remove brackets and normalize whitespace from a title string."""
    title = re.sub(r"[\[\](){}]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _strip_poster_search_prefix(title):
    title = str(title or "")
    # Unbracketed channel handles sometimes use underscores as the only
    # separator before a title. Stop at the first TitleCase word so the
    # handle cannot consume the complete filename.
    title = re.sub(
        r"^\s*[-_. ]*@[A-Za-z0-9_]{3,64}?(?=_[A-Z][a-z])[-_. ]*",
        "",
        title,
    )
    title = re.sub(
        r"^\s*[-_. ]*@[A-Za-z0-9_]{3,64}\s*(?:[-:|]+|[–—])\s*",
        "",
        title,
    )
    title = re.sub(
        r"^\s*(?:\[\s*S0*\d{1,2}\s*(?:-?\s*(?:E|EP)\s*\(?\s*0*\d{1,4}"
        r"\s*(?:-|\s)\s*0*\d{1,4}\s*\)?)\s*\]\s*)+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    uploader_pattern = "|".join(re.escape(tag) for tag in UPLOADER_TAGS)
    title = re.sub(
        rf"^\s*\[(?:{uploader_pattern})\]\s*[-_. ]*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"^\s*\[(?!S\d{1,2}\s*E\d{1,4}\])[^]]{1,40}\]\s*[-_. ]*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"^\s*[^\w\[\(]{1,12}\s*[-_. ]+", "", title, flags=re.UNICODE)
    title = re.sub(
        r"^\s*[A-Z0-9]{1,8}\s*[-_. ]+(?=\[?[Ss]\d{1,2}[\s._-]*[Ee]\d{1,4}\]?)",
        "",
        title,
    )
    return title.strip(" -._")


def is_hash_like_title(value):
    value = re.sub(r"(?i)[._ -]*part\s*\d+$", "", str(value or "").strip())
    text = re.sub(r"[^A-Za-z0-9]", "", value)
    if len(text) < 12:
        return False
    return bool(re.fullmatch(r"[a-fA-F0-9]{12,}", text))


def _first_caption_line(value):
    text = str(value or "").strip()
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("/"):
            return line
    return ""


def _usable_media_seed(value):
    text = str(value or "").strip()
    if not text:
        return ""
    text = _strip_poster_search_prefix(text)
    title, _, _ = format_clean_poster_title(text)
    if not title or is_hash_like_title(title):
        return ""
    meaningful = re.sub(TITLE_NOISE_PATTERN, " ", title, flags=re.IGNORECASE)
    meaningful = re.sub(
        r"(?i)\b(?:season|episode|ep|s\d+|e\d+)\b|\d+",
        " ",
        meaningful,
    )
    if len(re.sub(r"[^A-Za-z]", "", meaningful)) < 2:
        return ""
    if len(re.findall(r"[A-Za-z0-9]", title)) < 2:
        return ""
    return text


def choose_media_title_seed(filename, **extra):
    caption = _first_caption_line(extra.get("file_caption") or extra.get("precaption"))
    extracted = extra.get("first_file") or extra.get("extracted_name")
    merge_source = extra.get("merge_source_name") or extra.get("container_name")
    is_merged = bool(
        _extract_merge_range_metadata(filename)
        or _extract_merge_range_metadata(extra.get("source_filename"))
    )
    if is_merged:
        candidates = [
            merge_source,
            caption,
            extra.get("custom_name"),
            extracted,
            filename,
            extra.get("link"),
        ]
    elif extra.get("prefer_filename"):
        candidates = [
            filename,
            merge_source,
            extracted,
            caption,
            extra.get("custom_name"),
            extra.get("link"),
        ]
    else:
        candidates = [
            merge_source,
            extracted,
            caption,
            extra.get("custom_name"),
            filename,
            extra.get("link"),
        ]
    for candidate in candidates:
        seed = _usable_media_seed(candidate)
        if seed:
            return seed
    return filename


def format_clean_poster_title(raw_title, rename_regex=None):
    """Clean a raw filename into a search-friendly metadata lookup title.

    Returns (title, season_string_or_None, year_string_or_None).
    """
    from urllib.parse import unquote
    raw_title = unquote(raw_title)

    # Apply user's custom rename regex first if provided
    if rename_regex:
        try:
            raw_title = apply_regex_rename(raw_title, rename_regex)
        except Exception as e:
            LOGGER.warning(f"Failed to apply regex clean to TMDb title: {e}")
    raw_title = _strip_poster_search_prefix(raw_title)

    normalized = re.sub(r"https?://\S+", " ", raw_title)
    normalized = re.sub(r"\bt(?:elegram)?\.me/\S+", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bwww\S*", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\.\w{2,4}$", "", normalized)
    normalized = normalized.replace("_", " ").replace(".", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()

    season = None
    year = None
    merge_season_match = re.search(
        r"(?<![A-Za-z0-9])[Ss]0*(\d{1,2})[\s._-]*EP\s*\(?\d{1,4}",
        normalized,
        re.IGNORECASE,
    )
    if merge_season_match:
        season = f"Season {int(merge_season_match.group(1))}"
    season_match = re.search(
        r"(?<![A-Za-z0-9])(?:Season\s*|S)0*(\d{1,2})(?:\s*E\d{1,4})?(?![A-Za-z0-9])",
        normalized,
        re.IGNORECASE,
    )
    if season_match and not season:
        season = f"Season {int(season_match.group(1))}"
    year_match = re.search(r"\b(19\d{2}|20[0-3]\d)\b", normalized)
    if year_match:
        year = year_match.group(1)

    title = _clean_title_from_filename(raw_title)
    if title:
        if year:
            title = re.sub(rf"\b{re.escape(year)}\b", " ", title)
        title = re.sub(TITLE_NOISE_PATTERN, " ", title, flags=re.IGNORECASE)
        title = re.sub(
            r"(?i)(?<![A-Za-z0-9])(?:season\s*|s)0*\d{1,2}(?![A-Za-z0-9])",
            " ",
            title,
        )
        if _looks_like_anime_name(raw_title, title):
            title = re.sub(r"\s+\d{1,4}$", "", title).strip()
        title = _final_clean(re.sub(r"\s+", " ", title).strip(" -._"))
        if title:
            return title, season, year

    # Remove URLs and telegram links
    title = re.sub(r"https?://\S+", " ", raw_title)
    title = re.sub(r"\bt\.me/\S+", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\btelegram\.me/\S+", " ", title, flags=re.IGNORECASE)

    # Remove brackets early so start index checks are accurate
    title = re.sub(r"[\[\](){}]", " ", title)

    # Remove extension
    title = re.sub(r"\.\w{2,4}$", "", title)

    # Remove common domain names and standalone www
    title = re.sub(r"\b(www\.)?\w+\.(com|net|org|xyz|me|in|to|co|cc|info|tv|link|app|online|site|club|work|icu|top|vip|pro)\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\bwww\S*", " ", title, flags=re.IGNORECASE)

    # Replace dividers with space
    title = re.sub(r"[-_.]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

    season = None
    year = None

    sxx_exx = re.search(r"(?<!\w)S0*(\d{1,2})E\d{1,2}(?!\w)", title, re.IGNORECASE)
    if sxx_exx:
        season = f"Season {int(sxx_exx.group(1))}"
        if sxx_exx.start() <= 1:
            title = title[sxx_exx.end():].strip()
        else:
            title = title[: sxx_exx.start()].strip()
        return _final_clean(title), season, None

    season_match = re.search(r"\bSeason\s+(\d{1,2})\b", title, re.IGNORECASE)
    if season_match:
        season = f"Season {int(season_match.group(1))}"
        if season_match.start() <= 1:
            title = title[season_match.end():].strip()
        else:
            title = title[: season_match.start()].strip()
        return _final_clean(title), season, None

    s_simple = re.search(r"(?<!\w)S0*(\d{1,2})(?!\w)", title, re.IGNORECASE)
    if s_simple:
        season = f"Season {int(s_simple.group(1))}"
        if s_simple.start() <= 1:
            title = title[s_simple.end():].strip()
        else:
            title = title[: s_simple.start()].strip()
        return _final_clean(title), season, None

    ep_match = re.search(
        r"(?<!\w)(E\d{1,4}|EP\s*\d{1,4}|EPISODE\s*\d{1,4})(?!\w)",
        title,
        re.IGNORECASE,
    )
    if ep_match:
        if ep_match.start() <= 1:
            title = title[ep_match.end():].strip()
        else:
            title = title[: ep_match.start()].strip()

    all_years = list(re.finditer(r"\b(19|20)\d{2}\b", title))
    if all_years:
        last_year_match = all_years[-1]
        year = last_year_match.group(0)
        if last_year_match.start() <= 1:
            title = title[last_year_match.end():].strip()
        else:
            title = title[: last_year_match.start()].strip()
        return _final_clean(title), None, year

    return _final_clean(title), None, None


async def get_tmdb_poster_link(title, year=None, as_doc=False):
    """Fetch a poster/backdrop URL from TMDb with one live HTTP client."""
    access_token = Config.TMDB_ACCESS_TOKEN
    if not access_token:
        LOGGER.warning("TMDB_ACCESS_TOKEN not configured, skipping TMDb lookup")
        return None

    try:
        from httpx import AsyncClient, TimeoutException

        headers = {
            "Authorization": f"Bearer {access_token}",
            "accept": "application/json",
        }
        search_url = "https://api.themoviedb.org/3/search/multi"
        params = {
            "query": title,
            "include_adult": "false",
            "language": "en-US",
            "page": "1",
        }
        search_params_list = [params.copy()]
        if year:
            params["year"] = year
            params["primary_release_year"] = year
            params["first_air_date_year"] = year
            search_params_list = [params.copy()]
            no_year_params = params.copy()
            for year_key in ("year", "primary_release_year", "first_air_date_year"):
                no_year_params.pop(year_key, None)
            search_params_list.append(no_year_params)

        async with AsyncClient(timeout=10) as client:
            for search_params in search_params_list:
                for attempt in range(3):
                    try:
                        resp = await client.get(
                            search_url, params=search_params, headers=headers
                        )
                        if resp.status_code == 200:
                            results = resp.json().get("results", [])
                            if not results:
                                LOGGER.info(f"No TMDb results for '{title}'")
                                break

                            if year:
                                wanted_year = str(year)
                                first_result = next(
                                    (
                                        item
                                        for item in results
                                        if str(
                                            item.get("release_date")
                                            or item.get("first_air_date")
                                            or ""
                                        ).startswith(wanted_year)
                                    ),
                                    results[0],
                                )
                            else:
                                first_result = results[0]
                            tmdb_id = first_result.get("id")
                            media_type = first_result.get("media_type", "movie")
                            result_name = (
                                first_result.get("title")
                                or first_result.get("name")
                            )

                            if media_type == "person":
                                LOGGER.info(
                                    f"TMDb result is a person, skipping: {result_name}"
                                )
                                return None

                            images_url = (
                                f"https://api.themoviedb.org/3"
                                f"/{media_type}/{tmdb_id}/images"
                            )
                            img_resp = await client.get(
                                images_url,
                                params={"include_image_languages": "en,null"},
                                headers=headers,
                            )

                            if img_resp.status_code == 200:
                                img_data = img_resp.json()
                                backdrops = img_data.get("backdrops", [])
                                posters = img_data.get("posters", [])
                                en_backdrops = [
                                    b for b in backdrops
                                    if b.get("iso_639_1") == "en"
                                ]
                                clean_backdrops = [
                                    b for b in backdrops
                                    if b.get("iso_639_1") is None
                                ]
                                en_posters = [
                                    p for p in posters
                                    if p.get("iso_639_1") == "en"
                                ]
                                clean_posters = [
                                    p for p in posters
                                    if p.get("iso_639_1") is None
                                ]

                                image_path = None
                                image_type = "unknown"
                                if as_doc:
                                    choices = (
                                        (en_posters, "poster (en)"),
                                        (clean_posters, "poster (clean)"),
                                        (posters, "poster (other)"),
                                        (en_backdrops, "backdrop (en)"),
                                        (clean_backdrops, "backdrop (clean)"),
                                        (backdrops, "backdrop (other)"),
                                    )
                                else:
                                    choices = (
                                        (en_backdrops, "landscape (en)"),
                                        (clean_backdrops, "landscape (clean)"),
                                        (backdrops, "landscape (other)"),
                                    )
                                for images, img_type in choices:
                                    if images:
                                        image_path = images[0].get("file_path")
                                        image_type = img_type
                                        break

                                if image_path:
                                    LOGGER.info(
                                        f"Found TMDb {image_type}: {result_name}"
                                    )
                                    return (
                                        f"https://image.tmdb.org/t/p/original"
                                        f"{image_path}"
                                    )

                            LOGGER.info(
                                "Images endpoint failed, using search fallback"
                            )
                            backdrop_path = first_result.get("backdrop_path")
                            poster_path = first_result.get("poster_path")
                            fallback = (
                                (poster_path or backdrop_path) if as_doc
                                else backdrop_path
                            )
                            if fallback:
                                LOGGER.info(
                                    f"Found TMDb fallback image: {result_name}"
                                )
                                return (
                                    f"https://image.tmdb.org/t/p/original"
                                    f"{fallback}"
                                )

                            LOGGER.info(
                                f"No images available for '{title}' on TMDb"
                            )
                            return None

                        if resp.status_code == 401:
                            LOGGER.warning(
                                "TMDb authentication failed. Check your token"
                            )
                            return None
                        if resp.status_code >= 500:
                            LOGGER.warning(
                                f"TMDb server error {resp.status_code} "
                                f"(attempt {attempt + 1}/3)"
                            )
                            await sleep(2)
                            continue
                        LOGGER.warning(
                            f"TMDb API returned status {resp.status_code} "
                            f"for '{title}'"
                        )
                        return None
                    except TimeoutException:
                        LOGGER.warning(
                            f"Timeout on attempt {attempt + 1}/3 for TMDb API"
                        )
                    except Exception as e:
                        LOGGER.warning(
                            f"Client error on attempt {attempt + 1}/3: {e}"
                        )
                    await sleep(1)

    except Exception as e:
        LOGGER.error(f"TMDb API error for '{title}': {e}")
    return None


async def get_final_poster_url(raw_filename, as_doc=False, rename_regex=None):
    """Get the best poster URL for a given filename by searching TMDb.

    Extracts a clean title from the filename, then queries TMDb.
    Returns the poster URL string or None.
    """
    title, season, year = format_clean_poster_title(raw_filename, rename_regex)
    # Guard: skip TMDb search if title is empty or too short
    if not title or len(title.strip()) < 2:
        LOGGER.info(f"Title too short for TMDb search: '{title}'")
        return None
    LOGGER.info(f"Poster search title: {title}")
    if season:
        LOGGER.info(f"Season extracted: {season}")
    if year:
        LOGGER.info(f"Year extracted: {year}")

    cache_key = (
        f"poster:{title.lower()}:{str(year or '')}:"
        f"{'doc' if as_doc else 'media'}"
    )
    if cache_key in _metadata_cache:
        cached_url, provider = _metadata_cache[cache_key]
        LOGGER.info(f"Poster found via cached {provider}")
        return cached_url

    poster_url = await get_tmdb_poster_link(title, year, as_doc)
    if poster_url:
        LOGGER.info("Poster found via TMDb API")
        _metadata_cache[cache_key] = (poster_url, "TMDb")
        return poster_url

    anilist_url = await get_anilist_poster_link(title, as_doc)
    if anilist_url:
        LOGGER.info("Poster found via AniList API")
        _metadata_cache[cache_key] = (anilist_url, "AniList")
        return anilist_url
    mal_url = await get_mal_poster_link(title, as_doc)
    if mal_url:
        LOGGER.info("Poster found via MyAnimeList API")
        _metadata_cache[cache_key] = (mal_url, "MyAnimeList")
        return mal_url

    LOGGER.info("No poster found from metadata providers")
    return None


async def get_landscape_provider_thumbnail_url(raw_filename, rename_regex=None):
    title, _, year = format_clean_poster_title(raw_filename, rename_regex)
    if not title or len(title.strip()) < 2 or is_hash_like_title(title):
        return None
    tmdb_url = await get_tmdb_poster_link(title, year, as_doc=False)
    if tmdb_url:
        return tmdb_url
    return await get_anilist_poster_link(title, as_doc=False)


async def get_anime_landscape_thumbnail(video_file, raw_filename, duration=None, rename_regex=None, force=False):
    title, _, _ = format_clean_poster_title(raw_filename, rename_regex)
    if not force and not _looks_like_anime_name(raw_filename, title):
        return None

    poster_url = (
        await get_anilist_poster_link(title, as_doc=False)
        or await get_tmdb_poster_link(title, as_doc=False)
    )
    if poster_url:
        thumb = await download_image_thumb(poster_url, landscape=True)
        if thumb:
            LOGGER.info("Anime landscape thumbnail selected from metadata provider")
            return thumb

    LOGGER.info("Anime metadata thumbnail missing; continuing provider fallback")
    return None
