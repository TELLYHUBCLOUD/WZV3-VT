import json
import re
from contextlib import suppress
from asyncio import Event, create_subprocess_exec, create_task, sleep, wait_for
from asyncio.subprocess import PIPE
from os import path as ospath, walk
from time import time

from aiofiles import open as aiopen
from aiofiles.os import listdir, makedirs, path as aiopath, remove, rename

from ... import LOGGER, DOWNLOAD_DIR
from ...core.config_manager import BinConfig, Config
from ..ext_utils.bot_utils import cmd_exec, sync_to_async
from ..ext_utils.ffmpeg_queue import ffmpeg_task
from ..ext_utils.files_utils import get_source_container_name
from ..ext_utils.links_utils import is_url
from ..ext_utils.performance import get_ffmpeg_cores, get_ffmpeg_threads
from ..telegram_helper.message_utils import send_message

# Extensions that are valid for video tools
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".ts", ".m2ts", ".mpg", ".mpeg", ".vob", ".m4v", ".3gp",
    ".ogv", ".divx", ".rmvb", ".asf",
}
AUDIO_EXTENSIONS = {".aac", ".ac3", ".eac3", ".dts", ".flac", ".m4a", ".mka", ".mp3", ".ogg", ".opus", ".wav"}
SUBTITLE_EXTENSIONS = {".ass", ".srt", ".ssa", ".vtt"}

UI_TIMEOUT = 900  # 15 minutes

# Global dict to hold active video tool sessions: {task_id: event}
_active_vt_sessions = {}


def _merge_track_timeout():
    try:
        timeout = int(getattr(Config, "VT_MERGE_TRACK_TIMEOUT", 60) or 60)
    except (TypeError, ValueError):
        timeout = 60
    return max(10, timeout)


def _reply_timeout():
    try:
        timeout = int(getattr(Config, "VIDEO_TOOLS_REPLY_TIMEOUT", 30) or 30)
    except (TypeError, ValueError):
        timeout = 30
    return max(10, timeout)


def _video_tools_logs():
    return bool(getattr(Config, "VIDEO_TOOLS_LOGS", True))


def _lang_tokens(value):
    return [item.strip().lower() for item in re.split(r"[,|\s>]+", str(value or "")) if item.strip()]


def _lang_aliases(value):
    lang = str(value or "").strip().lower()
    aliases = {
        "ta": {"ta", "tam", "tamil"},
        "tam": {"ta", "tam", "tamil"},
        "tamil": {"ta", "tam", "tamil"},
        "te": {"te", "tel", "telugu"},
        "tel": {"te", "tel", "telugu"},
        "telugu": {"te", "tel", "telugu"},
        "en": {"en", "eng", "english"},
        "eng": {"en", "eng", "english"},
        "english": {"en", "eng", "english"},
        "hi": {"hi", "hin", "hindi"},
        "hin": {"hi", "hin", "hindi"},
        "hindi": {"hi", "hin", "hindi"},
    }
    return aliases.get(lang, {lang})


def _track_matches(track, wanted):
    if not wanted:
        return False
    values = _lang_aliases(track.get("lang")) | _lang_aliases(track.get("title"))
    return bool(values & wanted)


def _ordered_track_indexes(tracks, order_value, remove_unmatched=False):
    tokens = _lang_tokens(order_value)
    if not tokens:
        return [track["index"] for track in tracks]
    used = set()
    ordered = []
    for token in tokens:
        wanted = _lang_aliases(token)
        for track in tracks:
            idx = track["index"]
            if idx not in used and _track_matches(track, wanted):
                ordered.append(idx)
                used.add(idx)
    if not remove_unmatched:
        ordered.extend(track["index"] for track in tracks if track["index"] not in used)
    return ordered


def get_vt_event(task_id):
    session = _active_vt_sessions.get(task_id)
    return session["event"] if session else None


def get_vt_state(task_id):
    session = _active_vt_sessions.get(task_id)
    return session["state"] if session else None


async def probe_streams(file_path):
    """Use ffprobe to get audio and subtitle stream info."""
    result = await cmd_exec(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            file_path,
        ]
    )
    if not result[0] or result[2] != 0:
        return [], []

    try:
        data = json.loads(result[0])
    except (json.JSONDecodeError, Exception):
        return [], []

    audio_tracks = []
    sub_tracks = []

    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type", "")
        tags = stream.get("tags", {})
        lang = tags.get("language", "Unknown")
        title = tags.get("title", "")

        if codec_type == "audio":
            idx = len(audio_tracks)
            audio_tracks.append(
                {
                    "index": idx,
                    "stream_index": stream.get("index", idx),
                    "lang": lang,
                    "title": title or f"Audio {idx + 1}",
                    "codec": stream.get("codec_name", "unknown"),
                }
            )
        elif codec_type == "subtitle":
            idx = len(sub_tracks)
            sub_tracks.append(
                {
                    "index": idx,
                    "stream_index": stream.get("index", idx),
                    "lang": lang,
                    "title": title or f"Subtitle {idx + 1}",
                    "codec": stream.get("codec_name", "unknown"),
                }
            )

    return audio_tracks, sub_tracks


def _base_state(task_id, filename, audio_tracks, sub_tracks):
    return {
        "task_id": task_id,
        "filename": filename,
        "audio_tracks": audio_tracks,
        "sub_tracks": sub_tracks,
        "external_audio": [],
        "external_sub": [],
        "remove_audio": [],
        "remove_sub": [],
        "extract_audio": [],
        "extract_sub": [],
        "merge_audio": [],
        "merge_sub": [],
        "translate_sub": [],
        "keep_audio": [],
        "keep_sub": [],
        "audio_order": [],
        "sub_order": [],
        "default_audio": None,
        "default_sub": None,
        "intro_subtitle": False,
        "intro_text_available": False,
        "external_dir": "",
        "merge_intake": False,
        "merge_last_time": 0,
        "video_merge": False,
        "completed": False,
    }


async def _refresh_external_tracks(state, input_path):
    dir_path = ospath.dirname(input_path)
    input_name = ospath.basename(input_path)
    external_audio = []
    external_sub = []
    dirs = [dir_path]
    external_dir = state.get("external_dir")
    if external_dir and external_dir not in dirs:
        dirs.append(external_dir)

    for scan_dir in dirs:
        if not await aiopath.isdir(scan_dir):
            continue
        for name in sorted(await listdir(scan_dir)):
            if name == input_name or name.startswith("vt_") or name.startswith("intro_"):
                continue
            path = ospath.join(scan_dir, name)
            if not await aiopath.isfile(path):
                continue
            ext = ospath.splitext(name)[1].lower()
            if ext in AUDIO_EXTENSIONS:
                external_audio.append(
                    {"index": len(external_audio), "name": name, "path": path}
                )
            elif ext in VIDEO_EXTENSIONS:
                external_audio.append(
                    {"index": len(external_audio), "name": f"Audio from {name}", "path": path}
                )
            elif ext in SUBTITLE_EXTENSIONS:
                external_sub.append(
                    {"index": len(external_sub), "name": name, "path": path}
                )

    state["external_audio"] = external_audio
    state["external_sub"] = external_sub
    state["merge_audio"] = [
        idx for idx in state.get("merge_audio", []) if idx < len(external_audio)
    ]
    state["merge_sub"] = [
        idx for idx in state.get("merge_sub", []) if idx < len(external_sub)
    ]


async def _ensure_external_dir(state):
    if not state.get("external_dir"):
        state["external_dir"] = ospath.join(DOWNLOAD_DIR, f"vt_external_{state['task_id']}")
    await makedirs(state["external_dir"], exist_ok=True)
    return state["external_dir"]


def _select_all_external_tracks(state):
    state["merge_audio"] = [item["index"] for item in state.get("external_audio", [])]
    state["merge_sub"] = [item["index"] for item in state.get("external_sub", [])]


async def start_merge_track_intake(vt_msg, state, event):
    await _ensure_external_dir(state)
    state["merge_intake"] = True
    state["merge_last_time"] = time()
    timeout = _merge_track_timeout()
    await vt_msg.edit(
        "<b>Merge Tracks Intake</b>\n\n"
        f"Send audio, subtitle, or video files now. The bot waits {timeout} seconds after "
        "the last received file, then starts muxing automatically."
    )
    create_task(_merge_intake_timer(vt_msg, state, event))


async def _merge_intake_timer(vt_msg, state, event):
    while state.get("merge_intake") and not state.get("completed"):
        remaining = _merge_track_timeout() - int(time() - state.get("merge_last_time", time()))
        if remaining <= 0:
            break
        await sleep(min(remaining, 3))
    if state.get("completed"):
        return
    state["merge_intake"] = False
    _select_all_external_tracks(state)
    state["completed"] = True
    try:
        await vt_msg.edit("<b>Merge Tracks:</b> intake finished. Starting mux process...")
    except Exception:
        pass
    event.set()


def _message_merge_media(message):
    return (
        message.document
        or message.audio
        or message.video
        or message.animation
        or None
    )


def _find_merge_intake_session(message):
    user = message.from_user or message.sender_chat
    if not user:
        return None
    for item in _active_vt_sessions.values():
        listener = item.get("listener")
        state = item.get("state")
        if (
            listener
            and state
            and state.get("merge_intake")
            and getattr(listener, "user_id", None) == user.id
        ):
            return item
    return None


async def active_merge_track_filter(_, __, message):
    return bool(_message_merge_media(message) and _find_merge_intake_session(message))


async def video_tools_media_collector(client, message):
    session = _find_merge_intake_session(message)
    if not session:
        return
    state = session["state"]
    media = _message_merge_media(message)
    if not media:
        return
    filename = getattr(media, "file_name", "") or f"merge_track_{int(time())}"
    ext = ospath.splitext(filename)[1].lower()
    if ext not in AUDIO_EXTENSIONS and ext not in SUBTITLE_EXTENSIONS and ext not in VIDEO_EXTENSIONS:
        await send_message(message, "Video Tools: unsupported merge-track file type.")
        return
    external_dir = await _ensure_external_dir(state)
    target = ospath.join(external_dir, filename)
    downloaded = await message.download(file_name=target)
    if not downloaded:
        await send_message(message, "Video Tools: failed to download merge-track file.")
        return
    state["merge_last_time"] = time()
    if ext in AUDIO_EXTENSIONS:
        state["external_audio"].append(
            {"index": len(state["external_audio"]), "name": ospath.basename(downloaded), "path": downloaded}
        )
        state["merge_audio"].append(len(state["external_audio"]) - 1)
    elif ext in VIDEO_EXTENSIONS:
        state["external_audio"].append(
            {"index": len(state["external_audio"]), "name": f"Audio from {ospath.basename(downloaded)}", "path": downloaded}
        )
        state["merge_audio"].append(len(state["external_audio"]) - 1)
    elif ext in SUBTITLE_EXTENSIONS:
        state["external_sub"].append(
            {"index": len(state["external_sub"]), "name": ospath.basename(downloaded), "path": downloaded}
        )
        state["merge_sub"].append(len(state["external_sub"]) - 1)
    await send_message(
        message,
        f"Video Tools: received <code>{ospath.basename(downloaded)}</code>. Timer reset to {_merge_track_timeout()} seconds.",
    )


def _target_lang(listener):
    return (
        listener.user_dict.get("SUBTITLE_TRANSLATE_TARGET")
        or Config.SUBTITLE_TRANSLATE_TARGET
        or "en"
    ).strip()


def _srt_ts(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02},000"


def _ass_color(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith("&H"):
        return value
    if value.startswith("#") and len(value) == 7:
        r, g, b = value[1:3], value[3:5], value[5:7]
        return f"&H00{b}{g}{r}"
    return value


def _colorize_ass_letters(text, palette):
    colors = [_ass_color(item) for item in re.split(r"[\s,|]+", str(palette or "")) if item.strip()]
    colors = [item for item in colors if item]
    if not colors:
        return text
    out = []
    index = 0
    for char in text:
        if char in {"\\", "{", "}"}:
            out.append(char)
            continue
        if char.strip():
            out.append(f"{{\\c{colors[index % len(colors)]}}}{char}")
            index += 1
        else:
            out.append(char)
    return "".join(out)


async def _create_intro_subtitle(listener, dir_path):
    text = (
        listener.user_dict.get("INTRO_SUBTITLE_TEXT")
        or Config.INTRO_SUBTITLE_TEXT
        or ""
    ).strip()
    if not text:
        return None
    out_path = ospath.join(dir_path, f"intro_{listener.mid}.ass")
    duration = int(getattr(Config, "INTRO_SUBTITLE_DURATION", 5) or 5)
    fade_ms = int(getattr(Config, "INTRO_SUBTITLE_FADE_MS", 400) or 400)
    ranges = _parse_intro_ranges(
        listener.user_dict.get("INTRO_SUBTITLE_RANGES")
        or getattr(Config, "INTRO_SUBTITLE_RANGES", "")
    )
    font = str(getattr(Config, "INTRO_SUBTITLE_FONT", "Arial") or "Arial")
    size = int(getattr(Config, "INTRO_SUBTITLE_FONT_SIZE", 36) or 36)
    color = str(getattr(Config, "INTRO_SUBTITLE_COLOR", "&H00FFFFFF") or "&H00FFFFFF")
    outline = str(getattr(Config, "INTRO_SUBTITLE_OUTLINE_COLOR", "&H00000000") or "&H00000000")
    palette = str(getattr(Config, "INTRO_SUBTITLE_COLOR_PALETTE", "") or "")
    safe_text = text.replace("\n", r"\N").replace("{", "(").replace("}", ")")
    safe_text = _colorize_ass_letters(safe_text, palette)
    safe_text = r"{\b1}" + safe_text
    if not ranges:
        ranges = [(0, duration)]
    dialogues = []
    for start, end in ranges:
        if end <= start:
            continue
        dialogues.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Intro,,0,0,0,,{{\\fad({fade_ms},{fade_ms})}}{safe_text}"
        )
    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Intro,{font},{size},{color},{outline},1,3,1,2,40,40,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
{chr(10).join(dialogues)}
"""
    async with aiopen(out_path, "w", encoding="utf-8") as f:
        await f.write(ass)
    return out_path


def _ass_time(seconds):
    seconds = max(0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02}:{s:05.2f}"


def _parse_intro_ranges(value):
    text = str(value or "")
    ranges = []
    pattern = re.compile(
        r"(\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?)\s*-\s*(\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?)"
    )
    for start, end in pattern.findall(text):
        ranges.append((_clock_to_seconds(start), _clock_to_seconds(end)))
    return ranges


def _clock_to_seconds(value):
    value = str(value).replace(",", ".")
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _translate_srt_text(text, target):
    url = str(getattr(Config, "LIBRE_TRANSLATE_API_URL", "") or "").strip()
    key = str(getattr(Config, "LIBRE_TRANSLATE_API_KEY", "") or "").strip()
    if not url:
        raise RuntimeError("LIBRE_TRANSLATE_API_URL is not configured")

    import requests

    lines = text.splitlines()
    output = list(lines)
    indexes = []
    payload_lines = []
    for pos, line in enumerate(lines):
        stripped = line.strip()
        if (
            not stripped
            or stripped.isdigit()
            or "-->" in stripped
            or stripped.startswith(("{\\", "["))
        ):
            continue
        indexes.append(pos)
        payload_lines.append(line)

    if not payload_lines:
        return text if text.endswith("\n") else f"{text}\n"

    endpoint = url if url.rstrip("/").endswith("/translate") else f"{url.rstrip('/')}/translate"
    for start in range(0, len(payload_lines), 25):
        chunk = payload_lines[start : start + 25]
        joined = "\n".join(chunk)
        payload = {
            "q": joined,
            "source": "auto",
            "target": target,
            "format": "text",
        }
        if key:
            payload["api_key"] = key
        resp = requests.post(endpoint, json=payload, timeout=45)
        if resp.status_code != 200:
            body = resp.text[:200]
            if "api key" in body.lower():
                raise RuntimeError(
                    "LibreTranslate requires an API key. Set LIBRE_TRANSLATE_API_KEY "
                    "or use a LibreTranslate API URL that does not require a key."
                )
            raise RuntimeError(f"LibreTranslate API error {resp.status_code}: {body}")
        translated_text = resp.json().get("translatedText", "")
        translated_lines = translated_text.splitlines()
        if len(translated_lines) != len(chunk):
            translated_lines = [
                _translate_one_line(endpoint, line, target, key) for line in chunk
            ]
        for offset, translated in enumerate(translated_lines):
            output[indexes[start + offset]] = translated or chunk[offset]
    return "\n".join(output) + "\n"


def _translate_one_line(endpoint, line, target, key):
    import requests

    payload = {"q": line, "source": "auto", "target": target, "format": "text"}
    if key:
        payload["api_key"] = key
    resp = requests.post(endpoint, json=payload, timeout=30)
    if resp.status_code != 200:
        return line
    return resp.json().get("translatedText") or line


async def _translate_srt_file(src_path, target, task_id):
    dst_path = ospath.join(
        ospath.dirname(src_path),
        f"translated_{target}_{task_id}_{ospath.basename(src_path)}",
    )
    async with aiopen(src_path, encoding="utf-8", errors="ignore") as f:
        text = await f.read()
    translated = await sync_to_async(_translate_srt_text, text, target)
    async with aiopen(dst_path, "w", encoding="utf-8") as f:
        await f.write(translated)
    return dst_path


async def process_video_tool(listener, up_path):
    """
    Main entry point for video tools. Called from task_listener after download.
    Shows UI, waits for user input, then runs FFmpeg muxing.
    Returns the (possibly modified) up_path.
    """
    if getattr(listener, "_vt_processed", False):
        state = getattr(listener, "_vt_state", None)
        if state:
            if await aiopath.isdir(up_path):
                return await _process_vt_directory(listener, up_path, state)
            new_path = await _execute_vt_pipeline(listener, up_path, state)
            return new_path if new_path else up_path
        return up_path

    if await aiopath.isdir(up_path):
        return await _process_vt_directory(listener, up_path)

    # Only work on single files
    if not await aiopath.isfile(up_path):
        LOGGER.info("Video Tool: up_path is a directory, skipping.")
        await send_message(
            listener.message,
            "⚠️ <b>Video Tool:</b> Directories are not supported. Proceeding normally...",
        )
        return up_path

    ext = ospath.splitext(up_path)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        await send_message(
            listener.message,
            f"⚠️ <b>Video Tool:</b> <code>{ext}</code> is not a supported video format. Proceeding normally...",
        )
        return up_path

    # Probe the file
    audio_tracks, sub_tracks = await probe_streams(up_path)
    task_id = str(listener.mid)
    state = _base_state(task_id, ospath.basename(up_path), audio_tracks, sub_tracks)
    await _refresh_external_tracks(state, up_path)

    if (
        not audio_tracks
        and not sub_tracks
        and not state["external_audio"]
        and not state["external_sub"]
    ):
        await send_message(
            listener.message,
            "⚠️ <b>Video Tool:</b> No audio or subtitle streams found. Proceeding normally...",
        )
        return up_path

    # Store the state in the listener for the callback handler
    listener._vt_state = state

    # Create an event that the callback handler will set
    done_event = Event()
    _active_vt_sessions[task_id] = {
        "event": done_event,
        "state": state,
        "listener": listener,
    }

    try:
        # Render the UI
        from ...modules.video_tool_ui import render_video_tools_main

        tag = getattr(listener, "tag", None) or (listener.message.from_user.mention if listener.message.from_user else "")
        vt_msg = await send_message(
            listener.message,
            f"{tag} ⚙️ <b>Generating Video Tools UI...</b>",
        )
        listener._vt_msg = vt_msg
        await render_video_tools_main(vt_msg, state)

        # Wait for user to click Done/Close or timeout
        try:
            await wait_for(done_event.wait(), timeout=UI_TIMEOUT)
        except Exception:
            # Timeout - proceed with whatever was configured
            LOGGER.info(f"Video Tool timeout for task {task_id}, proceeding...")
            state["completed"] = True

        # If user cancelled (close), return original path
        if state.get("cancelled", False):
            return up_path

        # Execute the FFmpeg pipeline
        new_path = await _execute_vt_pipeline(listener, up_path, state)
        return new_path if new_path else up_path

    except Exception as e:
        LOGGER.error(f"Video Tool error: {e}")
        await send_message(
            listener.message,
            f"⚠️ <b>Video Tool Error:</b> <code>{e}</code>\nProceeding normally...",
        )
        return up_path
    finally:
        _active_vt_sessions.pop(task_id, None)
        listener._vt_state = None
        listener._vt_msg = None


async def _video_files(root):
    if await aiopath.isfile(root):
        return [root] if ospath.splitext(root)[1].lower() in VIDEO_EXTENSIONS else []
    files = []
    for dirpath, _, names in await sync_to_async(walk, root):
        for name in names:
            path = ospath.join(dirpath, name)
            if ospath.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
                files.append(path)
    return sorted(files, key=lambda p: ospath.basename(p).lower())


def _video_merge_requested(listener, up_path, state=None):
    if state and state.get("video_merge"):
        return True
    folder_name = getattr(listener, "folder_name", "") or ""
    base = ospath.basename(str(up_path).rstrip("/"))
    return folder_name.startswith("/vt_video_merge_") or base.startswith("vt_video_merge_")


async def _video_merge_signature(path):
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
    if result[2] != 0:
        return None
    try:
        data = json.loads(result[0] or "{}")
    except Exception:
        return None
    signature = []
    for stream in data.get("streams", []):
        if stream.get("codec_type") in {"video", "audio", "subtitle"}:
            signature.append(
                (
                    stream.get("codec_type"),
                    stream.get("codec_name"),
                    stream.get("width"),
                    stream.get("height"),
                    stream.get("sample_rate"),
                    stream.get("channels"),
                )
            )
    return signature


async def _validate_video_merge(videos):
    first = None
    mismatches = []
    for idx, path in enumerate(videos, start=1):
        sig = await _video_merge_signature(path)
        if not sig:
            mismatches.append(f"{idx}. {ospath.basename(path)} - unable to probe")
            continue
        if first is None:
            first = sig
        elif sig != first:
            mismatches.append(f"{idx}. {ospath.basename(path)} - stream layout mismatch")
    return mismatches


async def _send_clean_planner(listener, lines):
    await send_message(listener.message, "\n".join(lines))


async def _merge_video_directory(listener, root, videos):
    if len(videos) < 2:
        await send_message(listener.message, "Video + Video needs at least 2 video files.")
        return root

    planner = [
        "<b>Video Processing Plan</b>",
        "",
        "• Video + Video Merge: Enabled",
        f"• Total Videos: {len(videos)}",
        "• Mode: Concat demuxer, stream copy",
        "• Extra stream tools: Skipped for Video + Video",
        "",
        "<b>Order</b>",
    ]
    planner.extend(f"{idx}. <code>{ospath.basename(path)}</code>" for idx, path in enumerate(videos, start=1))
    await _send_clean_planner(listener, planner)

    mismatches = await _validate_video_merge(videos)
    if mismatches:
        await _send_clean_planner(
            listener,
            [
                "<b>Video + Video Merge cancelled</b>",
                "",
                "Inputs are not stream-copy compatible:",
                *mismatches[:10],
            ],
        )
        return root

    list_path = ospath.join(root, f"video_merge_{listener.mid}.ffconcat")
    source_name = get_source_container_name(
        getattr(listener, "merge_source_name", "")
    )
    output_name = f"{source_name or 'Video_Merge'}.mkv"
    output = ospath.join(root, output_name)
    if output in videos:
        output = ospath.join(root, f"{source_name or 'Video_Merge'} Merged.mkv")
    if await aiopath.exists(output):
        await remove(output)
    async with aiopen(list_path, "w", encoding="utf-8") as f:
        await f.write("ffconcat version 1.0\n")
        for path in videos:
            safe = path.replace("\\", "/").replace("'", "'\\''")
            await f.write(f"file '{safe}'\n")

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
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_path,
        "-c",
        "copy",
        "-threads",
        str(get_ffmpeg_threads()),
        "-max_muxing_queue_size",
        "9999",
        output,
    ]

    from ... import task_dict, task_dict_lock
    from ..ext_utils.files_utils import get_path_size
    from ..ext_utils.media_utils import FFMpeg, get_media_info
    from ..mirror_leech_utils.status_utils.ffmpeg_status import FFmpegStatus

    ffmpeg = FFMpeg(listener)
    ffmpeg.clear()
    total_duration = 0
    for video in videos:
        with suppress(Exception):
            total_duration += (await get_media_info(video))[0]
    ffmpeg._total_time = total_duration
    listener.subname = ospath.basename(output)
    listener.subsize = sum([await get_path_size(path) for path in videos])
    listener.progress = True
    async with task_dict_lock:
        task_dict[listener.mid] = FFmpegStatus(
            listener,
            ffmpeg,
            getattr(listener, "_process_gid", str(listener.mid)),
            "Video Tools",
        )

    try:
        async with ffmpeg_task(listener, "Video + Video merge"):
            listener.subproc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
            await ffmpeg._ffmpeg_progress()
            _, stderr = await listener.subproc.communicate()
        if listener.subproc.returncode != 0 or not await aiopath.exists(output):
            err = stderr.decode(errors="ignore").strip() if stderr else "Unknown error"
            LOGGER.error(f"Video + Video merge failed: {err}")
            await send_message(listener.message, f"Video + Video merge failed: <code>{err[:300]}</code>")
            return root
        for video in videos:
            if video != output and await aiopath.exists(video):
                await remove(video)
        await _send_clean_planner(
            listener,
            [
                "<b>Video Processing Plan</b>",
                "",
                "• Video + Video Merge: Enabled",
                "• FFmpeg Queue: Completed",
                "",
                "Processing Finished Successfully",
            ],
        )
        return output
    finally:
        if await aiopath.exists(list_path):
            await remove(list_path)


def _clone_state_for_path(source, filename, audio_tracks, sub_tracks):
    task_id = source["task_id"]
    state = _base_state(task_id, filename, audio_tracks, sub_tracks)
    audio_len = len(audio_tracks)
    sub_len = len(sub_tracks)
    for key in ("remove_audio", "extract_audio"):
        state[key] = [idx for idx in source.get(key, []) if idx < audio_len]
    for key in ("remove_sub", "extract_sub", "translate_sub"):
        state[key] = [idx for idx in source.get(key, []) if idx < sub_len]
    state["merge_audio"] = list(source.get("merge_audio", []))
    state["merge_sub"] = list(source.get("merge_sub", []))
    state["default_audio"] = (
        source.get("default_audio")
        if source.get("default_audio") is not None and source.get("default_audio") < audio_len
        else None
    )
    state["default_sub"] = (
        source.get("default_sub")
        if source.get("default_sub") is not None and source.get("default_sub") < sub_len
        else None
    )
    state["keep_audio"] = [idx for idx in source.get("keep_audio", []) if idx < audio_len]
    state["keep_sub"] = [idx for idx in source.get("keep_sub", []) if idx < sub_len]
    state["audio_order"] = [idx for idx in source.get("audio_order", []) if idx < audio_len]
    state["sub_order"] = [idx for idx in source.get("sub_order", []) if idx < sub_len]
    state["intro_subtitle"] = bool(source.get("intro_subtitle"))
    state["intro_text_available"] = bool(source.get("intro_text_available"))
    state["external_dir"] = source.get("external_dir", "")
    return state


async def _process_vt_directory(listener, up_path, pre_state=None):
    videos = await _video_files(up_path)
    if not videos:
        await send_message(
            listener.message,
            "<b>Video Tool:</b> No supported videos found after download/extract.",
        )
        return up_path

    if _video_merge_requested(listener, up_path, pre_state):
        return await _merge_video_directory(listener, up_path, videos)

    task_id = str(listener.mid)
    state = pre_state
    if state is None:
        first_video = None
        audio_tracks, sub_tracks = [], []
        for video in videos:
            audio_tracks, sub_tracks = await probe_streams(video)
            if audio_tracks or sub_tracks:
                first_video = video
                break
        if first_video is None:
            first_video = videos[0]
        state = _base_state(
            task_id, ospath.basename(first_video), audio_tracks, sub_tracks
        )
        await _refresh_external_tracks(state, first_video)

        if (
            not audio_tracks
            and not sub_tracks
            and not state["external_audio"]
            and not state["external_sub"]
        ):
            await send_message(
                listener.message,
                "<b>Video Tool:</b> No usable streams found in extracted videos.",
            )
            return up_path

        listener._vt_state = state
        done_event = Event()
        _active_vt_sessions[task_id] = {
            "event": done_event,
            "state": state,
            "listener": listener,
        }
        try:
            from ...modules.video_tool_ui import render_video_tools_main

            vt_msg = await send_message(
                listener.message,
                f"<b>Video Tools:</b> directory mode detected. Configure once; selection applies to {len(videos)} video(s).",
            )
            listener._vt_msg = vt_msg
            await render_video_tools_main(vt_msg, state)
            try:
                await wait_for(done_event.wait(), timeout=UI_TIMEOUT)
            except Exception:
                LOGGER.info(f"Video Tool directory timeout for task {task_id}, proceeding...")
                state["completed"] = True
            if state.get("cancelled", False):
                return up_path
        finally:
            _active_vt_sessions.pop(task_id, None)
            listener._vt_state = None
            listener._vt_msg = None

    await send_message(
        listener.message,
        f"Video Tools: processing {len(videos)} video(s) after download/extract...",
    )
    for video in videos:
        audio_tracks, sub_tracks = await probe_streams(video)
        work_state = _clone_state_for_path(
            state, ospath.basename(video), audio_tracks, sub_tracks
        )
        await _refresh_external_tracks(work_state, video)
        await _execute_vt_pipeline(listener, video, work_state)
        if listener.is_cancelled:
            break
    return up_path


async def _send_track_merge_planner(listener, input_path, state, extra_inputs, output_path):
    merge_audio = [item for item in state.get("external_audio", []) if item["index"] in state.get("merge_audio", [])]
    merge_sub = [item for item in state.get("external_sub", []) if item["index"] in state.get("merge_sub", [])]
    if not merge_audio and not merge_sub:
        return
    lines = [
        "<b>Video Tools Merge Planner</b>",
        f"Main: <code>{ospath.basename(input_path)}</code>",
        f"Output: <code>{ospath.basename(output_path)}</code>",
        "",
        "<b>Audio to mux:</b>",
    ]
    lines.extend(
        f"- <code>{item['name']}</code>" for item in merge_audio
    )
    if not merge_audio:
        lines.append("- none")
    lines.append("")
    lines.append("<b>Subtitles to mux:</b>")
    lines.extend(
        f"- <code>{item['name']}</code>" for item in merge_sub
    )
    if not merge_sub:
        lines.append("- none")
    lines.append("")
    lines.append("Action: stream-copy mux, no video re-encode unless FFmpeg requires it.")
    await send_message(listener.message, "\n".join(lines))


async def _execute_vt_pipeline(listener, input_path, state):
    """Run FFmpeg based on user selections from the VT UI."""
    await _refresh_external_tracks(state, input_path)
    has_removals = (
        state.get("remove_audio")
        or state.get("remove_sub")
        or state.get("keep_audio")
        or state.get("keep_sub")
        or state.get("audio_order")
        or state.get("sub_order")
        or state.get("audio_order_value")
        or state.get("sub_order_value")
    )
    has_default_audio = state.get("default_audio") is not None
    has_default_sub = state.get("default_sub") is not None
    has_extractions = state.get("extract_audio") or state.get("extract_sub")
    has_merge_audio = state.get("merge_audio")
    has_merge_sub = state.get("merge_sub")
    has_translate = state.get("translate_sub")
    has_intro = state.get("intro_subtitle") and (
        listener.user_dict.get("INTRO_SUBTITLE_TEXT")
        or Config.INTRO_SUBTITLE_TEXT
    )
    extract_only = bool(has_extractions) and not (
        has_removals
        or has_default_audio
        or has_default_sub
        or has_merge_audio
        or has_merge_sub
        or has_translate
        or has_intro
    )

    # Extract-only tasks return a small folder so the normal uploader sends only
    # the extracted streams, not the original video.
    if has_extractions:
        extracted_paths, extract_dir = await _extract_streams(listener, input_path, state)
        if extract_only:
            listener._vt_extract_only = True
            if extracted_paths:
                with suppress(Exception):
                    await remove(input_path)
            return extract_dir

    dir_path = ospath.dirname(input_path)
    extra_inputs = []
    cleanup_paths = []

    for idx in state.get("merge_audio", []):
        for item in state.get("external_audio", []):
            if item["index"] == idx:
                extra_inputs.append(("audio", item["path"]))
                cleanup_paths.append(item["path"])

    for idx in state.get("merge_sub", []):
        for item in state.get("external_sub", []):
            if item["index"] == idx:
                extra_inputs.append(("sub", item["path"]))
                cleanup_paths.append(item["path"])

    if has_translate:
        target = _target_lang(listener)
        all_sub = {t["index"]: t for t in state.get("sub_tracks", [])}
        for idx in state.get("translate_sub", []):
            sub_path = ospath.join(dir_path, f"subtitle_{listener.mid}_{idx}.srt")
            track = all_sub.get(idx)
            if track and await _extract_sub_as_srt(input_path, track, sub_path):
                try:
                    translated = await _translate_srt_file(
                        sub_path, target, listener.mid
                    )
                    extra_inputs.append(("sub", translated))
                    cleanup_paths.extend([sub_path, translated])
                except Exception as e:
                    LOGGER.warning(f"Subtitle translate failed: {e}")
                    await send_message(
                        listener.message,
                        f"Subtitle translate skipped: <code>{e}</code>",
                    )
                    if await aiopath.exists(sub_path):
                        await remove(sub_path)

    if has_intro:
        intro_path = await _create_intro_subtitle(listener, dir_path)
        if intro_path:
            extra_inputs.insert(0, ("intro_sub", intro_path))
            cleanup_paths.append(intro_path)

    # If no mux operations needed, return original
    if not (
        has_removals
        or has_default_audio
        or has_default_sub
        or extra_inputs
    ):
        return input_path

    # Build FFmpeg mux command
    base_name = ospath.basename(input_path)
    stem, ext = ospath.splitext(base_name)
    output_ext = ".mkv" if extra_inputs else ext
    output_path = ospath.join(dir_path, f"vt_{stem}{output_ext}")
    await _send_track_merge_planner(listener, input_path, state, extra_inputs, output_path)

    cmd = [
        "taskset",
        "-c",
        get_ffmpeg_cores(),
        BinConfig.FFMPEG_NAME,
        "-hide_banner", "-loglevel", "error",
        "-progress", "pipe:1",
        "-y", "-i", input_path,
    ]

    for _, path in extra_inputs:
        cmd.extend(["-i", path])

    cmd.extend(["-map", "0:v"])

    all_audio = {t["index"]: t for t in state["audio_tracks"]}
    all_sub = {t["index"]: t for t in state["sub_tracks"]}

    # Audio tracks to keep (respecting removals, keep-only, and order)
    audio_remove = set(state.get("remove_audio", []))
    if state.get("keep_audio"):
        audio_keep = [idx for idx in state["keep_audio"] if idx not in audio_remove]
    else:
        audio_keep = [t["index"] for t in state["audio_tracks"] if t["index"] not in audio_remove]
    if state.get("audio_order_value"):
        ordered = _ordered_track_indexes(
            [t for t in state["audio_tracks"] if t["index"] in audio_keep],
            state.get("audio_order_value"),
            remove_unmatched=bool(state.get("keep_audio")),
        )
        audio_keep = [idx for idx in ordered if idx in audio_keep]
    elif state.get("audio_order"):
        ordered = [idx for idx in state["audio_order"] if idx in audio_keep]
        ordered.extend(idx for idx in audio_keep if idx not in ordered)
        audio_keep = ordered

    for idx in audio_keep:
        cmd.extend(["-map", f"0:a:{idx}"])

    # Intro subtitles are mapped before existing subtitle streams so they open first.
    intro_sub_count = 0
    for input_idx, (kind, _) in enumerate(extra_inputs, start=1):
        if kind == "intro_sub":
            cmd.extend(["-map", f"{input_idx}:s?"])
            intro_sub_count += 1

    # Subtitle tracks to keep (respecting removals, keep-only, and order)
    sub_remove = set(state.get("remove_sub", []))
    if state.get("keep_sub"):
        sub_keep = [idx for idx in state["keep_sub"] if idx not in sub_remove]
    else:
        sub_keep = [t["index"] for t in state["sub_tracks"] if t["index"] not in sub_remove]
    if state.get("sub_order_value"):
        ordered = _ordered_track_indexes(
            [t for t in state["sub_tracks"] if t["index"] in sub_keep],
            state.get("sub_order_value"),
            remove_unmatched=bool(state.get("keep_sub")),
        )
        sub_keep = [idx for idx in ordered if idx in sub_keep]
    elif state.get("sub_order"):
        ordered = [idx for idx in state["sub_order"] if idx in sub_keep]
        ordered.extend(idx for idx in sub_keep if idx not in ordered)
        sub_keep = ordered

    for idx in sub_keep:
        cmd.extend(["-map", f"0:s:{idx}"])

    for input_idx, (kind, _) in enumerate(extra_inputs, start=1):
        if kind == "audio":
            cmd.extend(["-map", f"{input_idx}:a?"])
        elif kind == "sub":
            cmd.extend(["-map", f"{input_idx}:s?"])

    # Keep attachments and data streams
    cmd.extend(["-map", "0:t?", "-map", "0:d?"])

    # Copy all streams
    cmd.extend(
        ["-c", "copy", "-threads", str(get_ffmpeg_threads()), "-max_muxing_queue_size", "9999"]
    )

    # Reset all dispositions first
    cmd.extend(["-disposition:a", "0", "-disposition:s", "0"])

    # Set default audio
    default_audio = state.get("default_audio")
    if default_audio is not None and default_audio in audio_keep:
        out_idx = audio_keep.index(default_audio)
        cmd.extend([f"-disposition:a:{out_idx}", "default"])

    if intro_sub_count:
        cmd.extend(["-disposition:s:0", "default", "-metadata:s:s:0", "title=Intro"])

    # Set default subtitle
    default_sub = state.get("default_sub")
    if not intro_sub_count and default_sub is not None and default_sub in sub_keep:
        out_idx = sub_keep.index(default_sub)
        cmd.extend([f"-disposition:s:{out_idx}", "default"])

    cmd.append(output_path)

    LOGGER.info(f"Video Tool: Muxing {ospath.basename(input_path)}...")

    try:
        from ... import task_dict, task_dict_lock
        from ..ext_utils.files_utils import get_path_size
        from ..ext_utils.media_utils import FFMpeg, get_media_info
        from ..mirror_leech_utils.status_utils.ffmpeg_status import FFmpegStatus

        ffmpeg = FFMpeg(listener)
        ffmpeg.clear()
        duration = (await get_media_info(input_path))[0]
        ffmpeg._total_time = duration
        listener.subname = ospath.basename(input_path)
        listener.subsize = await get_path_size(input_path)
        listener.progress = True
        async with task_dict_lock:
            task_dict[listener.mid] = FFmpegStatus(
                listener,
                ffmpeg,
                getattr(listener, "_process_gid", str(listener.mid)),
                "Auto Process",
            )
        async with ffmpeg_task(listener, "Video Tools mux"):
            listener.subproc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
            await ffmpeg._ffmpeg_progress()
            _, stderr = await listener.subproc.communicate()
        process = listener.subproc

        if process.returncode != 0:
            err = stderr.decode().strip() if stderr else "Unknown error"
            LOGGER.error(f"FFmpeg muxing error: {err}")
            if await aiopath.exists(output_path):
                await remove(output_path)
            for path in cleanup_paths:
                if ospath.basename(path).startswith(("subtitle_", "translated_", "intro_")) and await aiopath.exists(path):
                    await remove(path)
            return None

        if not await aiopath.exists(output_path):
            LOGGER.error("FFmpeg muxing: output file not found")
            for path in cleanup_paths:
                if ospath.basename(path).startswith(("subtitle_", "translated_", "intro_")) and await aiopath.exists(path):
                    await remove(path)
            return None

        await remove(input_path)
        for path in cleanup_paths:
            if await aiopath.exists(path):
                await remove(path)
        if output_ext == ext:
            await rename(output_path, input_path)
            return input_path
        final_path = ospath.join(dir_path, f"{stem}{output_ext}")
        if await aiopath.exists(final_path):
            await remove(final_path)
        await rename(output_path, final_path)
        return final_path

    except Exception as e:
        LOGGER.error(f"FFmpeg muxing exception: {e}")
        if await aiopath.exists(output_path):
            await remove(output_path)
        for path in cleanup_paths:
            if ospath.basename(path).startswith(("subtitle_", "translated_", "intro_")) and await aiopath.exists(path):
                await remove(path)
        return None


async def _extract_streams(listener, input_path, state):
    """Extract selected audio/subtitle streams and return their output folder."""
    all_audio = {t["index"]: t for t in state["audio_tracks"]}
    all_sub = {t["index"]: t for t in state["sub_tracks"]}
    dir_path = ospath.dirname(input_path)
    stem = ospath.splitext(ospath.basename(input_path))[0]
    extract_dir = ospath.join(dir_path, f"vt_extract_{listener.mid}_{stem[:40]}")
    await makedirs(extract_dir, exist_ok=True)
    extracted = []
    failed = False

    for idx in state.get("extract_audio", []):
        track = all_audio.get(idx)
        if not track:
            continue
        ext = _extract_extension(track, "audio")
        out_name = f"Audio_{track['lang']}_{idx + 1}{ext}"
        out_path = ospath.join(extract_dir, out_name)
        if await _extract_single(input_path, track, out_path):
            extracted.append(out_path)
        else:
            failed = True

    for idx in state.get("extract_sub", []):
        track = all_sub.get(idx)
        if not track:
            continue
        ext = _extract_extension(track, "subtitle")
        out_name = f"Subtitle_{track['lang']}_{idx + 1}{ext}"
        out_path = ospath.join(extract_dir, out_name)
        if await _extract_single(input_path, track, out_path):
            extracted.append(out_path)
        else:
            failed = True
    if failed:
        await send_message(
            listener.message,
            "Extract failed: selected stream could not be copied. Codec/container mismatch or unsupported stream.",
        )
    return extracted, extract_dir


def _extract_extension(track, kind):
    codec = str(track.get("codec") or track.get("codec_name") or "").lower()
    if kind == "audio":
        return {
            "aac": ".aac",
            "eac3": ".eac3",
            "ac3": ".ac3",
            "dts": ".dts",
            "truehd": ".thd",
            "flac": ".flac",
            "opus": ".opus",
            "mp3": ".mp3",
            "vorbis": ".ogg",
        }.get(codec, f".{codec or 'audio'}")
    return {
        "subrip": ".srt",
        "ass": ".ass",
        "ssa": ".ass",
        "webvtt": ".vtt",
    }.get(codec, f".{codec or 'sub'}")


async def _extract_single(input_path, track, out_path):
    """Extract a single stream using FFmpeg."""
    stream_index = track.get("stream_index", track.get("index"))
    codec = str(track.get("codec") or track.get("codec_name") or "unknown")
    map_spec = f"0:{stream_index}"
    LOGGER.info(
        "VT extract stream: index=%s codec=%s ext=%s output=%s",
        stream_index,
        codec,
        ospath.splitext(out_path)[1],
        out_path,
    )
    cmd = [
        "taskset",
        "-c",
        get_ffmpeg_cores(),
        BinConfig.FFMPEG_NAME,
        "-y", "-i", input_path,
        "-map", map_spec,
        "-c", "copy",
        "-threads", str(get_ffmpeg_threads()),
        out_path,
    ]
    async with ffmpeg_task(label="Extract stream"):
        process = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        _, stderr = await process.communicate()
    LOGGER.info("VT extract stream return code: %s", process.returncode)
    valid_output = False
    if process.returncode == 0 and await aiopath.exists(out_path):
        try:
            valid_output = await aiopath.getsize(out_path) > 0
        except Exception:
            valid_output = False
    if not valid_output:
        with suppress(Exception):
            if await aiopath.exists(out_path):
                await remove(out_path)
        LOGGER.warning(
            "Stream extraction failed for %s codec=%s rc=%s stderr=%s",
            map_spec,
            codec,
            process.returncode,
            stderr.decode(errors="ignore")[-500:] if stderr else "",
        )
        return False
    return True


async def _extract_sub_as_srt(input_path, track, out_path):
    stream_index = track.get("stream_index", track.get("index"))
    map_spec = f"0:{stream_index}"
    cmd = [
        "taskset",
        "-c",
        get_ffmpeg_cores(),
        BinConfig.FFMPEG_NAME,
        "-y", "-i", input_path,
        "-map", map_spec,
        "-c:s", "srt",
        "-threads", str(get_ffmpeg_threads()),
        out_path,
    ]
    async with ffmpeg_task(label="Extract subtitle for translate"):
        process = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        _, stderr = await process.communicate()
    if process.returncode == 0 and await aiopath.exists(out_path):
        try:
            if await aiopath.getsize(out_path) > 0:
                return True
        except Exception:
            pass
    with suppress(Exception):
        if await aiopath.exists(out_path):
            await remove(out_path)
    LOGGER.warning(
        "Subtitle SRT extraction failed for %s codec=%s rc=%s stderr=%s",
        map_spec,
        track.get("codec"),
        process.returncode,
        stderr.decode(errors="ignore")[-500:] if stderr else "",
    )
    return False


async def pre_probe_and_show_ui(listener, file_, reply_to):
    """
    Tries to probe stream info before download starts (via streaming or chunks).
    If successful, shows the VT UI to the user to configure.
    """
    task_id = str(listener.mid)
    temp_dir = ospath.join(DOWNLOAD_DIR, f"vt_probe_{task_id}")
    temp_path = None

    async def _do_pre_probe():
        nonlocal temp_path
        audio_tracks, sub_tracks = [], []
        filename = "video"

        # 1. Telegram Media
        if file_ is not None:
            filename = file_.file_name or "video"
            await makedirs(temp_dir, exist_ok=True)
            temp_path = ospath.join(temp_dir, filename)

            from ...core.tg_client import TgClient
            async for chunk in TgClient.bot.stream_media(file_, limit=5):
                async with aiopen(temp_path, "ab") as f:
                    await f.write(chunk)

            audio_tracks, sub_tracks = await probe_streams(temp_path)

        # 2. Direct Link
        elif listener.link and is_url(listener.link):
            filename = ospath.basename(listener.link.split("?")[0]) or "video"

            # Try direct probe on link first
            audio_tracks, sub_tracks = await probe_streams(listener.link)

            # Fallback to downloading a 10MB chunk
            if not audio_tracks and not sub_tracks:
                await makedirs(temp_dir, exist_ok=True)
                temp_path = ospath.join(temp_dir, filename)
                headers = {
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }
                from aiohttp import ClientSession
                async with ClientSession() as session:
                    async with session.get(listener.link, headers=headers) as response:
                        async with aiopen(temp_path, "wb") as f:
                            async for chunk in response.content.iter_chunked(10000000):
                                await f.write(chunk)
                                break

                audio_tracks, sub_tracks = await probe_streams(temp_path)

        if not audio_tracks and not sub_tracks:
            raise ValueError("No audio or subtitle streams found in chunk")

        # Clean up temporary chunk file
        if temp_path and await aiopath.exists(temp_path):
            await remove(temp_path)

        state = _base_state(task_id, filename, audio_tracks, sub_tracks)
        state["pre_probed"] = True
        listener._vt_state = state
        listener._vt_processed = True

        done_event = Event()
        _active_vt_sessions[task_id] = {
            "event": done_event,
            "state": state,
            "listener": listener,
        }

        try:
            from ...modules.video_tool_ui import render_video_tools_main
            tag = getattr(listener, "tag", None) or (listener.message.from_user.mention if listener.message.from_user else "")
            vt_msg = await send_message(listener.message, f"{tag} ⚙️ <b>Generating Video Tools UI...</b>")
            listener._vt_msg = vt_msg
            await render_video_tools_main(vt_msg, state)

            try:
                await wait_for(done_event.wait(), timeout=UI_TIMEOUT)
            except Exception:
                LOGGER.info(f"Video Tool pre-probe interaction timeout for task {task_id}")
                state["completed"] = True

            if state.get("cancelled", False):
                listener._vt_state = None
            else:
                await vt_msg.delete()
        finally:
            _active_vt_sessions.pop(task_id, None)

    try:
        await wait_for(_do_pre_probe(), timeout=60.0)
    except Exception as e:
        LOGGER.warning(f"Video Tool: Pre-probing failed or timed out for task {task_id}: {e}. Falling back to full download first.")
        await send_message(
            listener.message,
            "Please wait for download to finish before opening Video Tools.",
        )
        if temp_path and await aiopath.exists(temp_path):
            await remove(temp_path)
