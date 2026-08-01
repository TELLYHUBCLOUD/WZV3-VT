import json
import re
from asyncio import create_subprocess_exec
from asyncio.subprocess import PIPE
from os import path as ospath, walk

from aiofiles import open as aiopen
from aiofiles.os import listdir, makedirs, path as aiopath, remove, rename

from ... import LOGGER
from ...core.config_manager import BinConfig, Config
from ...core.tg_client import TgClient
from ..ext_utils.bot_utils import cmd_exec, sync_to_async
from ..ext_utils.ffmpeg_queue import ffmpeg_task
from ..ext_utils.files_utils import (
    get_path_size,
    get_source_container_name,
    is_archive,
    is_supported_archive,
)
from ..ext_utils.media_utils import (
    extract_metadata_from_filename,
    format_clean_poster_title,
)
from ..telegram_helper.message_utils import edit_message, send_file, send_message
from .video_tools import (
    VIDEO_EXTENSIONS,
    _base_state,
    _execute_vt_pipeline,
    process_video_tool,
    probe_streams,
)


def _user_value(listener, key):
    if key in listener.user_dict:
        return listener.user_dict.get(key)
    return getattr(Config, key, None)


def auto_enabled(listener):
    return bool(listener.is_leech and _user_value(listener, "AUTO_PROCESS"))


def bool_setting(listener, key):
    if key == "AUTO_INTRO_SUBTITLE" and getattr(listener, "force_intro_subtitle", False):
        return True
    return bool(_user_value(listener, key))


def _quiet_messages():
    if not bool(getattr(Config, "AUTO_PROCESS_LOGS", False)):
        return True
    return str(getattr(Config, "AUTO_PROCESS_MESSAGE_MODE", "quiet") or "quiet").lower() == "quiet"


def _lang_set(value):
    if not value:
        return set()
    return {
        item.strip().lower()
        for item in re.split(r"[,|\s]+", str(value))
        if item.strip()
    }


def _lang_matches(track_lang, keep, track_title=""):
    if not keep:
        return False
    lang = str(track_lang or "").strip().lower()
    aliases = {
        "ta": {"ta", "tam", "tamil"},
        "tam": {"ta", "tam", "tamil"},
        "tamil": {"ta", "tam", "tamil"},
        "en": {"en", "eng", "english"},
        "eng": {"en", "eng", "english"},
        "english": {"en", "eng", "english"},
        "hi": {"hi", "hin", "hindi"},
        "hin": {"hi", "hin", "hindi"},
        "hindi": {"hi", "hin", "hindi"},
        "te": {"te", "tel", "telugu"},
        "tel": {"te", "tel", "telugu"},
        "telugu": {"te", "tel", "telugu"},
        "ja": {"ja", "jpn", "japanese"},
        "jpn": {"ja", "jpn", "japanese"},
        "japanese": {"ja", "jpn", "japanese"},
        "zh": {"chi", "zho", "zh", "chs", "cht", "cn", "chinese", "mandarin", "cantonese"},
        "zho": {"chi", "zho", "zh", "chs", "cht", "cn", "chinese", "mandarin", "cantonese"},
        "chi": {"chi", "zho", "zh", "chs", "cht", "cn", "chinese", "mandarin", "cantonese"},
        "chs": {"chi", "zho", "zh", "chs", "cht", "cn", "chinese", "mandarin", "cantonese"},
        "cht": {"chi", "zho", "zh", "chs", "cht", "cn", "chinese", "mandarin", "cantonese"},
        "cn": {"chi", "zho", "zh", "chs", "cht", "cn", "chinese", "mandarin", "cantonese"},
        "chinese": {"chi", "zho", "zh", "chs", "cht", "cn", "chinese", "mandarin", "cantonese"},
        "mandarin": {"chi", "zho", "zh", "chs", "cht", "cn", "chinese", "mandarin", "cantonese"},
        "cantonese": {"chi", "zho", "zh", "chs", "cht", "cn", "chinese", "mandarin", "cantonese"},
        "ko": {"kor", "ko", "kr", "korean"},
        "kor": {"kor", "ko", "kr", "korean"},
        "kr": {"kor", "ko", "kr", "korean"},
        "korean": {"kor", "ko", "kr", "korean"},
    }
    expanded_keep = set()
    for item in keep:
        expanded_keep |= aliases.get(item, {item})
        expanded_keep.add(item)
    track_values = aliases.get(lang, {lang}) | {lang}
    for token in re.findall(r"[a-z]{2,}|[\u3040-\u30ff\u4e00-\u9fff]+", str(track_title or "").lower()):
        track_values |= aliases.get(token, {token})
    return bool(track_values & expanded_keep)


async def _set_process_message(listener, text):
    msg = getattr(listener, "_auto_process_msg", None)
    if msg:
        res = await edit_message(msg, text)
        return msg if res is None or isinstance(res, str) else res
    msg = await send_message(listener.message, text)
    if not isinstance(msg, str):
        listener._auto_process_msg = msg
    return msg


async def _next_process_step(listener, phase, filename=""):
    listener._auto_process_step = getattr(listener, "_auto_process_step", 0) + 1
    if _quiet_messages():
        listener.subname = ospath.basename(filename) if filename else phase
        return
    total = getattr(listener, "_auto_process_total", 0)
    prefix = f"{listener._auto_process_step}/{total} " if total else ""
    text = f"Auto Process: {prefix}{phase}"
    if filename:
        text += f"\n<code>{ospath.basename(filename)}</code>"
    await _set_process_message(listener, text)


async def maybe_enable_auto_unzip(listener, up_path):
    if not auto_enabled(listener) or not bool_setting(listener, "AUTO_UNZIP"):
        return
    if listener.extract:
        return
    if await aiopath.isfile(up_path) and await is_supported_archive(up_path):
        listener.extract = True
        return
    if await aiopath.isdir(up_path):
        for name in await listdir(up_path):
            path = ospath.join(up_path, name)
            if await aiopath.isfile(path) and await is_supported_archive(path):
                listener.extract = True
                return


async def process_auto_pipeline(listener, up_path, gid):
    if not auto_enabled(listener):
        return up_path

    listener._process_gid = gid
    listener._auto_process_step = 0
    videos = await _video_files(up_path)
    auto_vt = bool_setting(listener, "AUTO_VT")
    auto_order = bool_setting(listener, "AUTO_ORDER")
    remove_streams = bool_setting(listener, "AUTO_REMOVE_STREAMS")
    auto_merge = bool_setting(listener, "AUTO_MERGE")
    auto_intro = bool_setting(listener, "AUTO_INTRO_SUBTITLE")
    listener._auto_process_total = (
        (1 if auto_vt else 0)
        + (len(videos) if auto_order else 0)
        + (len(videos) if _has_keep_filters(listener) and not remove_streams else 0)
        + (len(videos) if remove_streams else 0)
        + (1 if auto_merge and await aiopath.isdir(up_path) else 0)
        + (len(videos) if auto_intro else 0)
    )
    if not _quiet_messages():
        await _set_process_message(listener, "Auto Process: starting media pipeline...")

    if auto_vt:
        await _next_process_step(listener, "Opening Video Tools", up_path)
        up_path = await process_video_tool(listener, up_path)
        videos = await _video_files(up_path)
    else:
        if auto_order:
            up_path = await _auto_order_streams(listener, up_path)

        if _has_keep_filters(listener) and not remove_streams:
            up_path = await _auto_keep_streams(listener, up_path)

        if remove_streams:
            up_path = await _auto_remove_streams(listener, up_path)

    if auto_merge and await aiopath.isdir(up_path):
        up_path = await _smart_merge_directory(listener, up_path)

    if auto_intro:
        up_path = await _auto_intro(listener, up_path)

    if not _quiet_messages():
        await _set_process_message(listener, "Auto Process: processing finished. Starting upload...")
    return up_path


async def process_auto_finish_pipeline(listener, up_path, gid):
    if not auto_enabled(listener) and not getattr(listener, "force_intro_subtitle", False):
        return up_path
    listener._process_gid = gid
    listener._auto_process_step = 0
    if bool_setting(listener, "AUTO_INTRO_SUBTITLE"):
        up_path = await _auto_intro(listener, up_path)
    return up_path


async def _video_files(root):
    files = []
    if await aiopath.isfile(root):
        return [root] if ospath.splitext(root)[1].lower() in VIDEO_EXTENSIONS else []
    for dirpath, _, names in await sync_to_async(walk, root):
        for name in names:
            path = ospath.join(dirpath, name)
            if ospath.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
                files.append(path)
    return sorted(files, key=lambda p: ospath.basename(p).lower())


def _has_keep_filters(listener):
    return bool(
        _lang_set(_user_value(listener, "AUTO_KEEP_AUDIO_LANGS"))
        or _lang_set(_user_value(listener, "AUTO_KEEP_SUBTITLE_LANGS"))
    )


async def _auto_order_streams(listener, up_path):
    audio_order = _user_value(listener, "AUTO_AUDIO_ORDER")
    sub_order = _user_value(listener, "AUTO_SUBTITLE_ORDER")
    if not audio_order and not sub_order:
        return up_path

    changed = 0
    for video in await _video_files(up_path):
        audio_tracks, sub_tracks = await probe_streams(video)
        state = _base_state(str(listener.mid), ospath.basename(video), audio_tracks, sub_tracks)
        state["audio_order_value"] = audio_order or ""
        state["sub_order_value"] = sub_order or ""
        await _next_process_step(listener, "Ordering tracks", video)
        new_path = await _execute_vt_pipeline(listener, video, state)
        if new_path and new_path != video and up_path == video:
            up_path = new_path
        changed += 1

    if changed and _quiet_messages():
        await send_message(
            listener.message,
            "<b>Video Processing Plan</b>\n\n"
            f"• Track Order: {changed} file(s)\n"
            f"• Audio Order: <code>{audio_order or 'Default'}</code>\n"
            f"• Subtitle Order: <code>{sub_order or 'Default'}</code>\n"
            "• FFmpeg Queue: Completed\n\n"
            "Processing Finished Successfully",
        )
    return up_path


async def _auto_keep_streams(listener, up_path):
    keep_audio = _lang_set(_user_value(listener, "AUTO_KEEP_AUDIO_LANGS"))
    keep_sub = _lang_set(_user_value(listener, "AUTO_KEEP_SUBTITLE_LANGS"))
    if not keep_audio and not keep_sub:
        return up_path

    changed = 0
    for video in await _video_files(up_path):
        audio_tracks, sub_tracks = await probe_streams(video)
        state = _base_state(str(listener.mid), ospath.basename(video), audio_tracks, sub_tracks)
        if keep_audio:
            audio_keep = [
                t["index"]
                for t in audio_tracks
                if _lang_matches(t["lang"], keep_audio, t.get("title", ""))
            ]
            if audio_tracks and not audio_keep:
                await send_message(
                    listener.message,
                    f"Keep Audios skipped for <code>{ospath.basename(video)}</code>: no matching audio language found for <code>{', '.join(sorted(keep_audio))}</code>.",
                )
                continue
            state["keep_audio"] = audio_keep
        if keep_sub:
            sub_keep = [
                t["index"]
                for t in sub_tracks
                if _lang_matches(t["lang"], keep_sub, t.get("title", ""))
            ]
            if sub_tracks and not sub_keep:
                await send_message(
                    listener.message,
                    f"Keep Subtitles warning for <code>{ospath.basename(video)}</code>: no matching subtitle language found for <code>{', '.join(sorted(keep_sub))}</code>.",
                )
            else:
                state["keep_sub"] = sub_keep
        if state["keep_audio"] or state["keep_sub"]:
            await _next_process_step(listener, "Keeping selected streams", video)
            new_path = await _execute_vt_pipeline(listener, video, state)
            if new_path and new_path != video and up_path == video:
                up_path = new_path
            changed += 1

    if changed and _quiet_messages():
        await send_message(
            listener.message,
            "<b>Video Processing Plan</b>\n\n"
            f"• Keep Streams: {changed} file(s)\n"
            f"• Keep Audios: <code>{', '.join(sorted(keep_audio)) or 'Not Set'}</code>\n"
            f"• Keep Subtitles: <code>{', '.join(sorted(keep_sub)) or 'Not Set'}</code>\n"
            "• FFmpeg Queue: Completed\n\n"
            "Processing Finished Successfully",
        )
    return up_path


async def _auto_remove_streams(listener, up_path):
    if _has_keep_filters(listener) or _user_value(listener, "AUTO_AUDIO_ORDER") or _user_value(listener, "AUTO_SUBTITLE_ORDER") or bool_setting(listener, "AUTO_ORDER"):
        await send_message(
            listener.message,
            "Auto Remove Streams skipped: Auto Remove cannot run together with Keep/Order settings.",
        )
        return up_path
    await send_message(
        listener.message,
        "Auto Remove Streams skipped: no remove list is configured. Use Auto -vt or manual -vt to select streams.",
    )
    return up_path

    keep_audio = _lang_set(_user_value(listener, "AUTO_KEEP_AUDIO_LANGS"))
    keep_sub = _lang_set(_user_value(listener, "AUTO_KEEP_SUBTITLE_LANGS"))
    audio_order = _user_value(listener, "AUTO_AUDIO_ORDER")
    sub_order = _user_value(listener, "AUTO_SUBTITLE_ORDER")
    if not keep_audio and not keep_sub and not audio_order and not sub_order:
        await send_message(
            listener.message,
            "Auto Remove Streams skipped: set Keep Audios/Subtitles or Audios/Subtitles Order first.",
        )
        return up_path

    changed = 0
    for video in await _video_files(up_path):
        audio_tracks, sub_tracks = await probe_streams(video)
        state = _base_state(str(listener.mid), ospath.basename(video), audio_tracks, sub_tracks)
        state["audio_order_value"] = audio_order or ""
        state["sub_order_value"] = sub_order or ""
        if keep_audio:
            audio_keep = [
                t["index"]
                for t in audio_tracks
                if _lang_matches(t["lang"], keep_audio, t.get("title", ""))
            ]
            if audio_tracks and not audio_keep:
                await send_message(
                    listener.message,
                    f"Auto Remove Streams skipped for <code>{ospath.basename(video)}</code>: no matching audio language found for <code>{', '.join(sorted(keep_audio))}</code>.",
                )
                continue
            state["remove_audio"] = [
                t["index"] for t in audio_tracks if t["index"] not in audio_keep
            ]
        if keep_sub:
            sub_keep = [
                t["index"]
                for t in sub_tracks
                if _lang_matches(t["lang"], keep_sub, t.get("title", ""))
            ]
            if sub_tracks and not sub_keep:
                await send_message(
                    listener.message,
                    f"Auto Remove Streams warning for <code>{ospath.basename(video)}</code>: no matching subtitle language found for <code>{', '.join(sorted(keep_sub))}</code>. Keeping existing subtitles.",
                )
            else:
                state["remove_sub"] = [
                    t["index"] for t in sub_tracks if t["index"] not in sub_keep
                ]
        if state["remove_audio"] or state["remove_sub"] or audio_order or sub_order:
            await _next_process_step(listener, "Filtering streams", video)
            new_path = await _execute_vt_pipeline(listener, video, state)
            if new_path and new_path != video and up_path == video:
                up_path = new_path
            changed += 1
    if changed and _quiet_messages():
        await send_message(
            listener.message,
            "<b>Video Processing Plan</b>\n\n"
            f"• Stream Filter: {changed} file(s)\n"
            f"• Audio Order: <code>{audio_order or 'Default'}</code>\n"
            f"• Subtitle Order: <code>{sub_order or 'Default'}</code>\n"
            "• FFmpeg Queue: Completed\n\n"
            "Processing Finished Successfully",
        )
    return up_path


async def _auto_intro(listener, up_path):
    targets = await _video_files(up_path)
    changed = 0
    for video in targets:
        audio_tracks, sub_tracks = await probe_streams(video)
        state = _base_state(str(listener.mid), ospath.basename(video), audio_tracks, sub_tracks)
        state["intro_subtitle"] = True
        state["intro_text_available"] = True
        await _next_process_step(listener, "Adding intro subtitle", video)
        new_path = await _execute_vt_pipeline(listener, video, state)
        if new_path and new_path != video and up_path == video:
            up_path = new_path
        changed += 1
    if changed and _quiet_messages():
        await send_message(
            listener.message,
            f"Auto Process: intro subtitle finished for <code>{changed}</code> file(s).",
        )
    return up_path


async def _smart_merge_directory(listener, root):
    videos = await _video_files(root)
    if len(videos) < 2:
        return root

    items = []
    for path in videos:
        meta = await extract_metadata_from_filename(ospath.basename(path), path)
        size = await aiopath.getsize(path)
        items.append({"path": path, "meta": meta, "size": size})

    groups = {}
    for item in items:
        title = (item["meta"].get("title") or "Unknown").strip().lower()
        season = item["meta"].get("season") or "1"
        groups.setdefault((title, season), []).append(item)

    limit = _merge_limit(listener)
    batches = []
    warnings = []
    for (_, season), group_items in groups.items():
        group_items.sort(key=lambda i: _episode_no(i["meta"].get("episode")))
        current, current_size = [], 0
        for item in group_items:
            if item["size"] > limit:
                warnings.append(
                    f"Oversize single file, normal split will handle: {ospath.basename(item['path'])}"
                )
                if current:
                    batches.append(current)
                    current, current_size = [], 0
                batches.append([item])
                continue
            if current and current_size + item["size"] > limit:
                batches.append(current)
                current, current_size = [], 0
            current.append(item)
            current_size += item["size"]
        if current:
            batches.append(current)

    planner_path = await _write_planner(listener, root, batches, limit, warnings)
    await send_file(listener.message, planner_path, "Auto Merge planner")

    output_paths = []
    await makedirs(root, exist_ok=True)
    keep_sources = _has_keep_filters(listener)
    for batch in batches:
        output_paths.extend(await _merge_batch_checked(listener, root, batch, limit, keep_sources))

    if output_paths and not keep_sources:
        await _remove_non_outputs(root, set(output_paths), planner_path)
    if await aiopath.exists(planner_path):
        await remove(planner_path)
    return root


async def _merge_batch_checked(listener, root, batch, limit, keep_sources=False):
    out_path = await _merge_batch(listener, root, batch)
    if not out_path:
        return []
    try:
        out_size = await aiopath.getsize(out_path)
    except Exception:
        out_size = 0
    if out_size <= limit or len(batch) == 1:
        if not keep_sources:
            await _delete_batch_sources(batch, out_path)
        return [out_path]

    await send_message(
        listener.message,
        f"Auto Merge: output exceeded limit, moving last episode to next batch: <code>{ospath.basename(out_path)}</code>",
    )
    await remove(out_path)
    first = await _merge_batch_checked(listener, root, batch[:-1], limit, keep_sources)
    second = await _merge_batch_checked(listener, root, batch[-1:], limit, keep_sources)
    return first + second


async def _delete_batch_sources(batch, output):
    for item in batch:
        src = item["path"]
        if src != output and await aiopath.exists(src):
            await remove(src)


def _merge_limit(listener):
    safety = max(0, int(getattr(Config, "AUTO_MERGE_SAFETY_MB", 150) or 150))
    max_size = getattr(listener, "max_split_size", 0) or TgClient.MAX_SPLIT_SIZE
    return max(1, max_size - safety * 1024 * 1024)


def _episode_no(value):
    match = re.search(r"\d+", str(value or "0"))
    return int(match.group(0)) if match else 0


def _episode_text(value):
    no = _episode_no(value)
    return str(no).zfill(2)


def _season_text(value):
    try:
        return str(int(str(value or "1")))
    except ValueError:
        return str(value or "1")


def _clean_filename(name):
    name = re.sub(r'[\\/:*?"<>|]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180].strip()


def _batch_name(listener, batch):
    first = batch[0]
    last = batch[-1]
    meta = first["meta"].copy()
    season = _season_text(meta.get("season"))
    start = _episode_text(first["meta"].get("episode"))
    end = _episode_text(last["meta"].get("episode"))
    episode_range = f"{start}-{end}"
    range_tag = f"[S{season}-EP({episode_range})]"
    template = "{title} {resolution} {bit} {quality} {lib}"
    meta.update(
        {
            "start": start,
            "end": end,
            "episode": episode_range,
            "episodes": episode_range,
            "range": f"EP({episode_range})",
            "range_tag": range_tag,
        }
    )
    source_name = get_source_container_name(
        getattr(listener, "merge_source_name", "")
    )
    if source_name:
        source_title, _, _ = format_clean_poster_title(source_name)
        clean_source = source_title or source_name
        meta["title"] = clean_source
        meta["name"] = clean_source
    try:
        base = template.format_map({k: str(v or "") for k, v in meta.items()})
    except Exception:
        base = meta.get("title") or "Merged"
    base = _clean_filename(f"{range_tag} {base}")
    return f"{base}.mkv"


async def _write_planner(listener, root, batches, limit, warnings):
    path = ospath.join(root, f"auto_merge_planner_{listener.mid}.txt")
    lines = [
        "Auto Merge Planner",
        f"Merge limit: {limit} bytes",
        "",
    ]
    for idx, batch in enumerate(batches, start=1):
        name = _batch_name(listener, batch)
        total = sum(item["size"] for item in batch)
        lines.append(f"Batch {idx}: {name}")
        lines.append(f"Estimated size: {total} bytes")
        for item in batch:
            lines.append(f"  - {ospath.basename(item['path'])}")
        lines.append("")
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"- {w}" for w in warnings)
    async with aiopen(path, "w", encoding="utf-8") as f:
        await f.write("\n".join(lines))
    return path


async def _compatible(files):
    signature = None
    for path in files:
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
            return False
        try:
            data = json.loads(result[0] or "{}")
        except Exception:
            return False
        streams = data.get("streams", [])
        sig = []
        for stream in streams:
            if stream.get("codec_type") in {"video", "audio"}:
                sig.append(
                    (
                        stream.get("codec_type"),
                        stream.get("codec_name"),
                        stream.get("width"),
                        stream.get("height"),
                        stream.get("sample_rate"),
                        stream.get("channels"),
                    )
                )
        if signature is None:
            signature = sig
        elif signature != sig:
            return False
    return True


async def _merge_batch(listener, root, batch):
    output = ospath.join(root, _batch_name(listener, batch))
    if await aiopath.exists(output):
        await remove(output)
    if len(batch) == 1:
        src = batch[0]["path"]
        if src != output:
            await rename(src, output)
        return output

    list_path = ospath.join(root, f"concat_{listener.mid}_{len(batch)}.txt")
    async with aiopen(list_path, "w", encoding="utf-8") as f:
        for item in batch:
            safe = item["path"].replace("'", "'\\''")
            await f.write(f"file '{safe}'\n")

    copy_mode = await _compatible([item["path"] for item in batch])
    cmd = [
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
    ]
    if copy_mode:
        cmd.extend(["-c", "copy"])
    else:
        cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-c:s", "copy"])
    cmd.extend(["-max_muxing_queue_size", "9999", output])

    await _next_process_step(listener, "Merging batch", output)
    from ... import task_dict, task_dict_lock
    from ..ext_utils.media_utils import FFMpeg, get_media_info
    from ..mirror_leech_utils.status_utils.ffmpeg_status import FFmpegStatus

    ffmpeg = FFMpeg(listener)
    ffmpeg.clear()
    total_duration = 0
    for item in batch:
        try:
            total_duration += (await get_media_info(item["path"]))[0]
        except Exception:
            pass
    ffmpeg._total_time = total_duration
    listener.subname = ospath.basename(output)
    listener.subsize = sum(item["size"] for item in batch)
    listener.progress = True
    async with task_dict_lock:
        task_dict[listener.mid] = FFmpegStatus(
            listener,
            ffmpeg,
            getattr(listener, "_process_gid", str(listener.mid)),
            "Auto Process",
        )
    async with ffmpeg_task(listener, "Auto merge"):
        listener.subproc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        await ffmpeg._ffmpeg_progress()
        _, stderr = await listener.subproc.communicate()
    process = listener.subproc
    await remove(list_path)
    if process.returncode != 0 or not await aiopath.exists(output):
        LOGGER.error(f"Auto merge failed: {stderr.decode(errors='ignore')}")
        return None
    return output


async def _remove_non_outputs(root, outputs, planner_path):
    for dirpath, _, names in await sync_to_async(walk, root):
        for name in names:
            path = ospath.join(dirpath, name)
            if path in outputs or path == planner_path:
                continue
            if ospath.splitext(name)[1].lower() not in VIDEO_EXTENSIONS:
                try:
                    await remove(path)
                except Exception:
                    pass
