import json
from asyncio import Event, create_subprocess_exec, sleep, wait_for
from asyncio.subprocess import PIPE
from os import path as ospath

from aiofiles.os import makedirs, path as aiopath, remove

from ... import LOGGER, cores
from ...core.config_manager import BinConfig
from ..ext_utils.bot_utils import cmd_exec
from ..telegram_helper.message_utils import send_message

# Extensions that are valid for video tools
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".ts", ".m2ts", ".mpg", ".mpeg", ".vob", ".m4v", ".3gp",
    ".ogv", ".divx", ".rmvb", ".asf",
}

UI_TIMEOUT = 300  # 5 minutes

# Global dict to hold active video tool sessions: {task_id: event}
_active_vt_sessions = {}


def get_vt_event(task_id):
    return _active_vt_sessions.get(task_id)


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


async def process_video_tool(listener, up_path):
    """
    Main entry point for video tools. Called from task_listener after download.
    Shows UI, waits for user input, then runs FFmpeg muxing.
    Returns the (possibly modified) up_path.
    """
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

    if not audio_tracks and not sub_tracks:
        await send_message(
            listener.message,
            "⚠️ <b>Video Tool:</b> No audio or subtitle streams found. Proceeding normally...",
        )
        return up_path

    task_id = str(listener.mid)
    state = {
        "task_id": task_id,
        "filename": ospath.basename(up_path),
        "audio_tracks": audio_tracks,
        "sub_tracks": sub_tracks,
        "remove_audio": [],
        "remove_sub": [],
        "extract_audio": [],
        "extract_sub": [],
        "swap_audio": {},
        "default_audio": None,
        "default_sub": None,
        "completed": False,
    }

    # Store the state in the listener for the callback handler
    listener._vt_state = state

    # Create an event that the callback handler will set
    done_event = Event()
    _active_vt_sessions[task_id] = done_event

    try:
        # Render the UI
        from ...modules.video_tool_ui import render_video_tools_main

        vt_msg = await send_message(
            listener.message,
            "⚙️ <b>Generating Video Tools UI...</b>",
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


async def _execute_vt_pipeline(listener, input_path, state):
    """Run FFmpeg based on user selections from the VT UI."""
    has_removals = state.get("remove_audio") or state.get("remove_sub")
    has_swap = bool(state.get("swap_audio"))
    has_default_audio = state.get("default_audio") is not None
    has_default_sub = state.get("default_sub") is not None
    has_extractions = state.get("extract_audio") or state.get("extract_sub")

    # Extract streams first (upload them individually)
    if has_extractions:
        await _extract_streams(listener, input_path, state)

    # If no mux operations needed, return original
    if not (has_removals or has_swap or has_default_audio or has_default_sub):
        return input_path

    # Build FFmpeg mux command
    dir_path = ospath.dirname(input_path)
    base_name = ospath.basename(input_path)
    output_path = ospath.join(dir_path, f"vt_{base_name}")

    cmd = [
        "taskset",
        "-c",
        cores,
        BinConfig.FFMPEG_NAME,
        "-hide_banner", "-loglevel", "error",
        "-y", "-i", input_path,
        "-map", "0:v",
    ]

    all_audio = {t["index"]: t for t in state["audio_tracks"]}
    all_sub = {t["index"]: t for t in state["sub_tracks"]}

    # Audio tracks to keep (respecting removals + swap order)
    audio_remove = set(state.get("remove_audio", []))
    audio_keep = [t["index"] for t in state["audio_tracks"] if t["index"] not in audio_remove]

    swap_dict = state.get("swap_audio", {})
    if swap_dict:
        audio_keep.sort(key=lambda idx: swap_dict.get(str(idx), 999))

    for idx in audio_keep:
        cmd.extend(["-map", f"0:a:{idx}"])

    # Subtitle tracks to keep
    sub_remove = set(state.get("remove_sub", []))
    sub_keep = [t["index"] for t in state["sub_tracks"] if t["index"] not in sub_remove]

    for idx in sub_keep:
        cmd.extend(["-map", f"0:s:{idx}"])

    # Keep attachments and data streams
    cmd.extend(["-map", "0:t?", "-map", "0:d?"])

    # Copy all streams
    cmd.extend(["-c", "copy"])

    # Reset all dispositions first
    cmd.extend(["-disposition:a", "0", "-disposition:s", "0"])

    # Set default audio
    default_audio = state.get("default_audio")
    if default_audio is not None and default_audio in audio_keep:
        out_idx = audio_keep.index(default_audio)
        cmd.extend([f"-disposition:a:{out_idx}", "default"])

    # Set default subtitle
    default_sub = state.get("default_sub")
    if default_sub is not None and default_sub in sub_keep:
        out_idx = sub_keep.index(default_sub)
        cmd.extend([f"-disposition:s:{out_idx}", "default"])

    cmd.append(output_path)

    LOGGER.info(f"Video Tool: Muxing {ospath.basename(input_path)}...")

    try:
        process = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        _, stderr = await process.communicate()

        if process.returncode != 0:
            err = stderr.decode().strip() if stderr else "Unknown error"
            LOGGER.error(f"FFmpeg muxing error: {err}")
            if await aiopath.exists(output_path):
                await remove(output_path)
            return None

        if not await aiopath.exists(output_path):
            LOGGER.error("FFmpeg muxing: output file not found")
            return None

        # Remove original, rename output to original name
        await remove(input_path)
        from aiofiles.os import rename
        await rename(output_path, input_path)
        return input_path

    except Exception as e:
        LOGGER.error(f"FFmpeg muxing exception: {e}")
        if await aiopath.exists(output_path):
            await remove(output_path)
        return None


async def _extract_streams(listener, input_path, state):
    """Extract selected audio/subtitle streams and upload them."""
    all_audio = {t["index"]: t for t in state["audio_tracks"]}
    all_sub = {t["index"]: t for t in state["sub_tracks"]}
    dir_path = ospath.dirname(input_path)

    for idx in state.get("extract_audio", []):
        track = all_audio.get(idx)
        if not track:
            continue
        out_name = f"Audio_{track['lang']}_{idx + 1}.aac"
        out_path = ospath.join(dir_path, out_name)
        await _extract_single(input_path, f"0:a:{idx}", out_path)

    for idx in state.get("extract_sub", []):
        track = all_sub.get(idx)
        if not track:
            continue
        out_name = f"Subtitle_{track['lang']}_{idx + 1}.srt"
        out_path = ospath.join(dir_path, out_name)
        await _extract_single(input_path, f"0:s:{idx}", out_path)


async def _extract_single(input_path, map_spec, out_path):
    """Extract a single stream using FFmpeg."""
    cmd = [
        "taskset",
        "-c",
        cores,
        BinConfig.FFMPEG_NAME,
        "-y", "-i", input_path,
        "-map", map_spec,
        "-c", "copy",
        out_path,
    ]
    process = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    await process.communicate()
    if not await aiopath.exists(out_path):
        LOGGER.warning(f"Stream extraction failed for {map_spec}")
