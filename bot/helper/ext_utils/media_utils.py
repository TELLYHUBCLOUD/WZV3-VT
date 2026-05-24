import re
from contextlib import suppress
from PIL import Image
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

from ... import LOGGER, DOWNLOAD_DIR, threads, cores
from ...core.config_manager import BinConfig, Config
from .bot_utils import cmd_exec, sync_to_async
from .files_utils import get_mime_type, is_archive, is_archive_split
from .status_utils import time_to_seconds


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


async def download_image_thumb(url):
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
                    im.convert("RGB").save(dst, "JPEG", quality=95, optimize=True)
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
                f"{cores}",
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
                f"{threads}",
                output,
            ]
            cap_time += interval
            cmds.append(cmd_exec(cmd))
        try:
            resutls = await wait_for(gather(*cmds), timeout=60)
            if resutls[0][2] != 0:
                LOGGER.error(
                    f"Error while creating screenshots from video. Path: {video_file}. stderr: {resutls[0][1]}"
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
        f"{cores}",
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
        f"{threads}",
        output,
    ]
    try:
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
    duration = duration // 2
    cmd = [
        "taskset",
        "-c",
        f"{cores}",
        BinConfig.FFMPEG_NAME,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{duration}",
        "-i",
        video_file,
        "-vf",
        "thumbnail",
        "-q:v",
        "1",
        "-frames:v",
        "1",
        "-threads",
        f"{threads}",
        output,
    ]
    try:
        _, err, code = await wait_for(cmd_exec(cmd), timeout=60)
        if code != 0 or not await aiopath.exists(output):
            LOGGER.error(
                f"Error while extracting thumbnail from video. Name: {video_file} stderr: {err}"
            )
            return None
    except Exception:
        LOGGER.error(
            f"Error while extracting thumbnail from video. Name: {video_file}. Error: Timeout some issues with ffmpeg with specific arch!"
        )
        return None
    return output


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
        f"{cores}",
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
        f"{threads}",
        output,
    ]
    try:
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
                f"{cores}",
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
                f"{threads}",
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
                f"{cores}",
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
                f"{threads}",
                output,
            ]
        if self._listener.is_cancelled:
            return False
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
            f"{cores}",
            BinConfig.FFMPEG_NAME,
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            audio_file,
            "-threads",
            f"{threads}",
            output,
        ]
        if self._listener.is_cancelled:
            return False
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
            f"{cores}",
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
            f"{threads}",
            output_file,
        ]

        if self._listener.is_cancelled:
            return False
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
            out_path = f_path.replace(file_, f"{base_name}.part{i:03}{extension}")
            cmd = [
                "taskset",
                "-c",
                f"{cores}",
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
                f"{threads}",
                out_path,
            ]
            if not multi_streams:
                del cmd[15]
                del cmd[15]
            if self._listener.is_cancelled:
                return False
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


async def extract_metadata_from_filename(filename, filepath=None):
    """Extract title, season, episode, quality, and chapter from a filename.

    Ported from WZMLakane leech_utils.py with extended patterns for anime,
    TV shows, movies, and manga chapter naming conventions.
    """
    metadata = {
        "title": "Unknown",
        "season": "1",
        "episode": "01",
        "quality": "1080p",
        "chapter": "001",
    }

    uploader_tags = [
        "Toonworld4all",
        "SubsPlease",
        "EMBER",
        "Erai-raws",
        "HorribleSubs",
        "AnimeRG",
        "Judas",
        "ASW",
        "Anime Time",
    ]

    pattern = (
        r"^\[(?:" + "|".join(re.escape(tag) for tag in uploader_tags) + r")\]\s*"
    )
    clean_filename = re.sub(pattern, "", filename, flags=re.IGNORECASE).strip()

    title_patterns = [
        r"^(.+?)[\s\.\-]*[Ss]0*(\d+)[\s\.\-]*[Ee]0*(\d+)",
        r"^(.+?)[\s\.\-]*[Ss]eason[\s\.\-]*0*(\d+)[\s\.\-]*[Ee]pisode[\s\.\-]*0*(\d+)",
        r"^\[CH[-\s]?\d+\][\s\.\-]*(.+?)[\s\.\-]*-",
        r"^\[\d+\][\s\.\-]*(.+?)[\s\.\-]*(?:@|$)",
        r"^(.+?)[\s\.\-]*(?:Ch(?:apter)?|#)[\s\.\-]*\d+",
        r"^(.+?)[\s\.\-]*\[",
        r"^0*(\d{1,4})[\s\.\-_]+(.+?)(?:[\s\.\-_]+|@|$)",
    ]

    title_found = False
    episode_found = False

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
            metadata["title"] = title
            title_found = True
            break

    if not title_found and clean_filename:
        base_title = re.split(
            r"[\.\-\s]+(?:199[0-9]|20[0-2][0-9]|2030)|[\.\-\s]+\d{3,4}p",
            clean_filename,
        )[0]
        if base_title:
            base_title = base_title.replace(".", " ").replace("-", " ").strip()
            base_title = re.sub(
                r"\s*[\(\[]?\s*(199[0-9]|20[0-2][0-9]|2030)\s*[\)\]]?\s*",
                " ",
                base_title,
            ).strip()
            metadata["title"] = base_title

    season_match = re.search(r"[Ss](?:eason[\s\.\-]*)?0*(\d+)", filename)
    if season_match:
        metadata["season"] = season_match.group(1)

    if not episode_found:
        episode_patterns = [
            r"^0*(\d{1,4})[\s\.\-_]+(?![xX]\d)",
            r"[Ee](?:pisode|p)?[\s\.\-]*0*(\d+)",
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

    quality_match = re.search(r"(\d{3,4}p|4K|2160p)", filename, re.IGNORECASE)
    if quality_match:
        metadata["quality"] = quality_match.group(1)
    elif filepath and await aiopath.exists(filepath):
        file_size = await aiopath.getsize(filepath)
        if file_size > 500 * 1024 * 1024:
            metadata["quality"] = "HDRip"

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
        r"\bS\d+E(\d+)\b",
        r"\[(?:C|c)(\d+)\]",
    ]

    for pat in chapter_patterns:
        chapter_match = re.search(pat, filename, re.IGNORECASE)
        if chapter_match:
            metadata["chapter"] = chapter_match.group(1).zfill(3)
            break

    return metadata


async def apply_template_rename(filename, template, filepath=None):
    """Apply a template-based rename using metadata extracted from the filename.

    Supports math offset tags like {episode:+12} or {season:-1}.
    Returns the renamed filename, preserving the original extension.
    """
    if not template or "{" not in template:
        return filename
    metadata = await extract_metadata_from_filename(filename, filepath)

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
        renamed = template.format(**metadata)
        original_ext = Path(filename).suffix
        if not renamed.endswith(original_ext):
            renamed += original_ext
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


def format_clean_poster_title(raw_title, rename_regex=None):
    """Clean a raw filename into a search-friendly title for TMDb lookup.

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
    """Fetch a poster/backdrop URL from TMDb API with language priority.

    Uses Config.TMDB_ACCESS_TOKEN for authentication.
    Two-step process:
    1. Search /search/multi to get TMDb ID + media_type
    2. Fetch /{media_type}/{id}/images for language-specific images

    Priority logic:
    - Video (as_doc=False): English backdrop > clean backdrop > any poster
    - Document (as_doc=True): English poster > any poster > any backdrop
    Returns the image URL string or None.
    """
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

        # Step 1: Search for the title
        search_url = "https://api.themoviedb.org/3/search/multi"
        params = {
            "query": title,
            "include_adult": "false",
            "language": "en-US",
            "page": "1",
        }
        if year:
            params["year"] = year

        for attempt in range(3):
            try:
                async with AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        search_url, params=params, headers=headers
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        results = data.get("results", [])
                        if not results:
                            LOGGER.info(f"No TMDb results for '{title}'")
                            return None

                        first_result = results[0]
                        tmdb_id = first_result.get("id")
                        media_type = first_result.get("media_type", "movie")
                        result_name = (
                            first_result.get("title")
                            or first_result.get("name")
                        )

                        # Skip person results
                        if media_type == "person":
                            LOGGER.info(
                                f"TMDb result is a person, skipping: {result_name}"
                            )
                            return None

                        # Step 2: Get images with language filter
                        images_url = (
                            f"https://api.themoviedb.org/3"
                            f"/{media_type}/{tmdb_id}/images"
                        )
                        images_params = {
                            "include_image_languages": "en,null",
                        }
                        img_resp = await client.get(
                            images_url,
                            params=images_params,
                            headers=headers,
                        )

                        if img_resp.status_code == 200:
                            img_data = img_resp.json()
                            backdrops = img_data.get("backdrops", [])
                            posters = img_data.get("posters", [])

                            # Separate English and clean (null) images
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
                            other_posters = [
                                p for p in posters
                                if p.get("iso_639_1") is None
                            ]

                            image_path = None
                            image_type = "unknown"

                            if as_doc:
                                # Document: English poster > any poster > backdrop
                                if en_posters:
                                    image_path = en_posters[0]["file_path"]
                                    image_type = "poster (en)"
                                elif other_posters:
                                    image_path = other_posters[0]["file_path"]
                                    image_type = "poster (clean)"
                                elif posters:
                                    image_path = posters[0]["file_path"]
                                    image_type = "poster (other)"
                                elif en_backdrops:
                                    image_path = en_backdrops[0]["file_path"]
                                    image_type = "backdrop (en)"
                                elif clean_backdrops:
                                    image_path = clean_backdrops[0]["file_path"]
                                    image_type = "backdrop (clean)"
                            else:
                                # Video: English backdrop > clean backdrop > poster
                                if en_backdrops:
                                    image_path = en_backdrops[0]["file_path"]
                                    image_type = "landscape (en)"
                                elif clean_backdrops:
                                    image_path = clean_backdrops[0]["file_path"]
                                    image_type = "landscape (clean)"
                                elif backdrops:
                                    image_path = backdrops[0]["file_path"]
                                    image_type = "landscape (other)"
                                elif en_posters:
                                    image_path = en_posters[0]["file_path"]
                                    image_type = "poster (en)"
                                elif posters:
                                    image_path = posters[0]["file_path"]
                                    image_type = "poster (fallback)"

                            if image_path:
                                poster_url = (
                                    f"https://image.tmdb.org/t/p/original"
                                    f"{image_path}"
                                )
                                LOGGER.info(
                                    f"Found TMDb {image_type}: {result_name}"
                                )
                                return poster_url

                        # Fallback: use search result's default image
                        LOGGER.info(
                            "Images endpoint failed, using search fallback"
                        )
                        backdrop_path = first_result.get("backdrop_path")
                        poster_path = first_result.get("poster_path")
                        fallback = (
                            (poster_path or backdrop_path) if as_doc
                            else (backdrop_path or poster_path)
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

                    elif resp.status_code == 401:
                        LOGGER.warning(
                            "TMDb authentication failed. Check your token"
                        )
                        return None
                    elif resp.status_code >= 500:
                        LOGGER.warning(
                            f"TMDb server error {resp.status_code} "
                            f"(attempt {attempt + 1}/3)"
                        )
                        await sleep(2)
                    else:
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

    poster_url = await get_tmdb_poster_link(title, year, as_doc)
    if poster_url:
        LOGGER.info("Poster found via TMDb API")
        return poster_url

    LOGGER.info("No poster found from TMDb")
    return None

