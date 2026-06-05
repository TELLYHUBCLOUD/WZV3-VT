from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import LOGGER
from ..helper.video_utils.video_tools import (
    UI_TIMEOUT,
    get_vt_event,
    get_vt_state,
    start_merge_track_intake,
)


def _selected_icon(selected):
    return "🟢 " if selected else ""


def _has_extract_selection(state):
    return bool(state.get("extract_audio") or state.get("extract_sub"))


def _clear_non_extract_tools(state):
    for key in (
        "remove_audio",
        "remove_sub",
        "merge_audio",
        "merge_sub",
        "translate_sub",
        "keep_audio",
        "keep_sub",
        "audio_order",
        "sub_order",
    ):
        state[key] = []
    state["audio_order_value"] = ""
    state["sub_order_value"] = ""
    state["default_audio"] = None
    state["default_sub"] = None
    state["intro_subtitle"] = False
    state["video_merge"] = False


def _tracks_for(state, action_key):
    if action_key == "merge_audio":
        return state.get("external_audio", [])
    if action_key == "merge_sub":
        return state.get("external_sub", [])
    if "audio" in action_key:
        return state.get("audio_tracks", [])
    return state.get("sub_tracks", [])


def _track_text(track):
    if "name" in track:
        return track["name"]
    return f"Track {track['index'] + 1} - {str(track.get('lang', 'unk')).upper()} ({track.get('codec', '')})"


async def render_video_tools_main(vt_msg, state):
    extract_selected = _has_extract_selection(state)
    text = (
        "<b>Video Tools Configuration</b>\n\n"
        f"<b>File:</b> <code>{state['filename']}</code>\n\n"
        f"<b>Audio Tracks:</b> {len(state['audio_tracks'])}\n"
        f"<b>Subtitles:</b> {len(state['sub_tracks'])}\n"
        f"<b>External Audio:</b> {len(state.get('external_audio', []))}\n"
        f"<b>External Subtitles:</b> {len(state.get('external_sub', []))}\n"
        f"<b>Video + Video:</b> {'On' if state.get('video_merge') else 'Off'}\n\n"
        f"<b>Timeout:</b> {UI_TIMEOUT} sec"
    )
    task_id = state["task_id"]
    if extract_selected:
        text += "\n\n<b>Extract mode:</b> enabled. Other Video Tools actions are disabled."
        rows = [
            [InlineKeyboardButton("🟢 Extract Stream", callback_data=f"vt_extract_{task_id}")],
            [
                InlineKeyboardButton("Done", callback_data=f"vt_done_{task_id}"),
                InlineKeyboardButton("Close", callback_data=f"vt_close_{task_id}"),
            ],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton("Remove Stream", callback_data=f"vt_remove_{task_id}"),
                InlineKeyboardButton("Extract Stream", callback_data=f"vt_extract_{task_id}"),
            ],
            [InlineKeyboardButton("Change Order", callback_data=f"vt_order_{task_id}")],
            [
                InlineKeyboardButton("Merge Tracks", callback_data=f"vt_merge_{task_id}"),
                InlineKeyboardButton("Translate Subs", callback_data=f"vt_translate_{task_id}"),
            ],
            [InlineKeyboardButton("Video + Video", callback_data=f"vt_video_{task_id}")],
            [
                InlineKeyboardButton("Done", callback_data=f"vt_done_{task_id}"),
                InlineKeyboardButton("Close", callback_data=f"vt_close_{task_id}"),
            ],
        ]
    markup = InlineKeyboardMarkup(rows)
    try:
        await vt_msg.edit(text, reply_markup=markup)
    except Exception as e:
        LOGGER.error(f"render_video_tools_main error: {e}")


async def render_change_order(query, state):
    task_id = state["task_id"]
    await query.message.edit_text(
        "<b>Change Order</b>\n\nChoose the stream type to reorder.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Audio Order", callback_data=f"vt_orderaudio_{task_id}"),
                    InlineKeyboardButton("Subtitle Order", callback_data=f"vt_ordersub_{task_id}"),
                ],
                [InlineKeyboardButton("Back", callback_data=f"vt_main_{task_id}")],
            ]
        ),
    )


async def render_stream_type_menu(query, task_id, mode, title):
    await query.message.edit_text(
        f"<b>{title}</b>\n\nChoose which stream type to configure:",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Audio", callback_data=f"vt_list_{mode}_audio_{task_id}"),
                    InlineKeyboardButton("Subtitle", callback_data=f"vt_list_{mode}_sub_{task_id}"),
                ],
                [InlineKeyboardButton("Back", callback_data=f"vt_main_{task_id}")],
            ]
        ),
    )


async def render_stream_list(query, state, action_key, title):
    task_id = state["task_id"]
    tracks = _tracks_for(state, action_key)
    if action_key in ("default_audio", "default_sub"):
        remove_key = "remove_audio" if "audio" in action_key else "remove_sub"
        tracks = [track for track in tracks if track["index"] not in state.get(remove_key, [])]

    if not tracks:
        await query.message.edit_text(
            "<b>No tracks found</b> for this type.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Back", callback_data=f"vt_main_{task_id}")]]
            ),
        )
        return

    markup = []
    selected = state.get(action_key, None)
    is_order = action_key in ("audio_order", "sub_order")
    for track in tracks:
        idx = track["index"]
        if action_key in ("default_audio", "default_sub"):
            icon = _selected_icon(selected == idx)
        elif is_order:
            order = state.get(action_key, [])
            icon = f"{order.index(idx) + 1}. 🟢 " if idx in order else ""
        else:
            icon = _selected_icon(idx in state.get(action_key, []))
        markup.append(
            [
                InlineKeyboardButton(
                    icon + _track_text(track),
                    callback_data=f"vt_toggle_{action_key}_{idx}_{task_id}",
                )
            ]
        )

    if action_key not in ("default_audio", "default_sub"):
        all_indices = [track["index"] for track in tracks]
        all_selected = len(state.get(action_key, [])) == len(all_indices)
        markup.append(
            [
                InlineKeyboardButton(
                    "Clear Multi" if all_selected else "Multi Select",
                    callback_data=f"vt_toggle_{action_key}_all_{task_id}",
                )
            ]
        )

    if action_key in ("remove_audio", "remove_sub"):
        markup.append(
            [
                InlineKeyboardButton("Reset", callback_data=f"vt_reset_{action_key}_{task_id}"),
                InlineKeyboardButton("Reverse", callback_data=f"vt_reverse_{action_key}_{task_id}"),
            ]
        )
        markup.append(
            [
                InlineKeyboardButton("Remove", callback_data=f"vt_removego_{task_id}"),
                InlineKeyboardButton("Continue", callback_data=f"vt_main_{task_id}"),
            ]
        )
    else:
        back = "vt_order_" + task_id if is_order else "vt_main_" + task_id
        markup.append([InlineKeyboardButton("Back", callback_data=back)])

    note = ""
    if action_key in ("keep_audio", "keep_sub") and state.get(action_key):
        note = f"\n\n<b>Keep only:</b> {len(state[action_key])} selected stream(s)."
    elif is_order and state.get(action_key):
        note = f"\n\n<b>Order selected:</b> {len(state[action_key])} stream(s)."
    elif action_key in ("remove_audio", "remove_sub") and state.get(action_key):
        note = f"\n\n<b>Selected to remove:</b> {len(state[action_key])} stream(s)."

    await query.message.edit_text(
        f"<b>{title}</b>\n\nSelect streams. Changes are saved automatically.{note}",
        reply_markup=InlineKeyboardMarkup(markup),
    )


async def video_tools_callback(_, query):
    data = query.data
    try:
        parts = data.split("_")
        if len(parts) < 3:
            raise ValueError("callback data too short")
        task_id = parts[-1]
        action = parts[1]
    except (ValueError, IndexError) as e:
        LOGGER.error(f"VT callback bad data: {data} - {e}")
        await query.answer(f"Bad callback: {e}", show_alert=True)
        return

    event = get_vt_event(task_id)
    if event is None:
        await query.answer("Session expired or already processed!", show_alert=True)
        return
    state = get_vt_state(task_id)
    if state is None:
        await query.answer("Session state not found!", show_alert=True)
        return
    if state.get("completed"):
        await query.answer("This task is already executed!", show_alert=True)
        return

    await query.answer()

    try:
        if action == "close":
            state["completed"] = True
            state["cancelled"] = True
            event.set()
            await query.message.edit_text("<b>Video Tools cancelled.</b> Proceeding normally...")
            return

        if action == "done":
            state["completed"] = True
            event.set()
            await query.message.edit_text("<b>Video Tools configuration saved.</b> Processing...")
            return

        if action == "removego":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with other tools.", show_alert=True)
                return
            state["completed"] = True
            event.set()
            await query.message.edit_text("<b>Video Tools:</b> removing selected streams...")
            return

        if action == "video":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Video + Video.", show_alert=True)
                return
            state["video_merge"] = True
            state["completed"] = True
            event.set()
            await query.message.edit_text("<b>Video + Video:</b> merge planner will start after download/extract.")
            return

        if action == "main":
            await render_video_tools_main(query.message, state)
            return

        if action == "remove":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Remove Stream.", show_alert=True)
                return
            await render_stream_type_menu(query, task_id, "remove", "Remove Stream")
            return

        if action == "extract":
            _clear_non_extract_tools(state)
            await render_stream_type_menu(query, task_id, "extract", "Extract Stream")
            return

        if action == "merge":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Merge Tracks.", show_alert=True)
                return
            await start_merge_track_intake(query.message, state, event)
            return

        if action == "translate":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Translate Subs.", show_alert=True)
                return
            await render_stream_list(query, state, "translate_sub", "Translate Subtitles")
            return

        if action == "keepaudio":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Keep Audios.", show_alert=True)
                return
            await render_stream_list(query, state, "keep_audio", "Keep Audios")
            return

        if action == "keepsub":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Keep Subtitles.", show_alert=True)
                return
            await render_stream_list(query, state, "keep_sub", "Keep Subtitles")
            return

        if action == "order":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Change Order.", show_alert=True)
                return
            await render_change_order(query, state)
            return

        if action == "orderaudio":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Audio Order.", show_alert=True)
                return
            await render_stream_list(query, state, "audio_order", "Audio Order")
            return

        if action == "ordersub":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Subtitle Order.", show_alert=True)
                return
            await render_stream_list(query, state, "sub_order", "Subtitle Order")
            return

        if action == "defa":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Default Audio.", show_alert=True)
                return
            await render_stream_list(query, state, "default_audio", "Default Audio")
            return

        if action == "defs":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Default Subtitle.", show_alert=True)
                return
            await render_stream_list(query, state, "default_sub", "Default Subtitle")
            return

        if action == "list":
            mode = parts[2]
            track_type = parts[3]
            action_key = f"{mode}_{track_type}"
            if _has_extract_selection(state) and not action_key.startswith("extract_"):
                await query.answer("Extract Stream cannot be combined with other tools.", show_alert=True)
                return
            title = {
                "remove_audio": "Remove Audio",
                "remove_sub": "Remove Subtitle",
                "extract_audio": "Extract Audio",
                "extract_sub": "Extract Subtitle",
            }.get(action_key, f"{mode.title()} {track_type.title()}")
            await render_stream_list(query, state, action_key, title)
            return

        if action == "toggle":
            action_key = parts[2] + "_" + parts[3]
            idx_str = parts[4]
            if _has_extract_selection(state) and not action_key.startswith("extract_"):
                await query.answer("Extract Stream cannot be combined with other tools.", show_alert=True)
                return
            tracks = _tracks_for(state, action_key)
            all_indices = [track["index"] for track in tracks]

            if idx_str == "all":
                if action_key in ("default_audio", "default_sub"):
                    state[action_key] = None
                else:
                    state[action_key] = [] if len(state.get(action_key, [])) == len(all_indices) else list(all_indices)
            else:
                idx = int(idx_str)
                if action_key in ("default_audio", "default_sub"):
                    state[action_key] = None if state[action_key] == idx else idx
                elif action_key in ("audio_order", "sub_order"):
                    state.setdefault(action_key, [])
                    if idx in state[action_key]:
                        state[action_key].remove(idx)
                    else:
                        state[action_key].append(idx)
                else:
                    state.setdefault(action_key, [])
                    if idx in state[action_key]:
                        state[action_key].remove(idx)
                    else:
                        state[action_key].append(idx)
                    if action_key == "remove_audio" and state.get("default_audio") == idx:
                        state["default_audio"] = None
                    if action_key == "remove_sub" and state.get("default_sub") == idx:
                        state["default_sub"] = None
            if action_key.startswith("extract_") and state.get(action_key):
                _clear_non_extract_tools(state)

            title_map = {
                "remove_audio": "Remove Audio",
                "remove_sub": "Remove Subtitle",
                "extract_audio": "Extract Audio",
                "extract_sub": "Extract Subtitle",
                "merge_audio": "Merge Audio Files",
                "merge_sub": "Merge Subtitle Files",
                "translate_sub": "Translate Subtitles",
                "keep_audio": "Keep Audios",
                "keep_sub": "Keep Subtitles",
                "audio_order": "Audio Order",
                "sub_order": "Subtitle Order",
                "default_audio": "Default Audio",
                "default_sub": "Default Subtitle",
            }
            await render_stream_list(query, state, action_key, title_map[action_key])
            return

        if action in ("reset", "reverse"):
            action_key = parts[2] + "_" + parts[3]
            tracks = _tracks_for(state, action_key)
            if action == "reset":
                state[action_key] = []
            else:
                all_indices = [track["index"] for track in tracks]
                state[action_key] = [
                    idx for idx in all_indices if idx not in state.get(action_key, [])
                ]
            title_map = {
                "remove_audio": "Remove Audio",
                "remove_sub": "Remove Subtitle",
            }
            await render_stream_list(query, state, action_key, title_map.get(action_key, "Remove Stream"))
    except Exception as e:
        err_msg = f"{type(e).__name__}: {e}"
        LOGGER.error(f"VT callback error: action={action} data={data} - {e}")
        try:
            await query.answer(err_msg[:200], show_alert=True)
        except Exception:
            pass
