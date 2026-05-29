from time import time

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import LOGGER
from ..helper.video_utils.video_tools import get_vt_event


async def render_video_tools_main(vt_msg, state):
    """Render the main Video Tools menu."""
    text = (
        f"<b>🎬 Video Tools Configuration</b>\n\n"
        f"<b>File:</b> <code>{state['filename']}</code>\n\n"
        f"<b>Audio Tracks:</b> {len(state['audio_tracks'])}\n"
        f"<b>Subtitles:</b> {len(state['sub_tracks'])}\n\n"
        f"⏳ <b>Timeout:</b> 300 sec"
    )

    task_id = state["task_id"]

    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🗑️ Remove Stream", callback_data=f"vt_remove_{task_id}"
                ),
                InlineKeyboardButton(
                    "📤 Extract Stream", callback_data=f"vt_extract_{task_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔀 Audio Swap", callback_data=f"vt_swap_{task_id}"
                ),
                InlineKeyboardButton(
                    "🎧 Default Audio", callback_data=f"vt_defa_{task_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔠 Default Subtitle", callback_data=f"vt_defs_{task_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "✅ Done", callback_data=f"vt_done_{task_id}"
                ),
                InlineKeyboardButton(
                    "❌ Close", callback_data=f"vt_close_{task_id}"
                ),
            ],
        ]
    )

    try:
        await vt_msg.edit(text, reply_markup=markup)
    except Exception as e:
        LOGGER.error(f"render_video_tools_main error: {e}")


async def render_stream_list(query, state, action_key, title):
    """Render the stream list for a specific action."""
    task_id = state["task_id"]
    is_audio = "audio" in action_key
    tracks = state["audio_tracks"] if is_audio else state["sub_tracks"]

    # Filter out removed tracks for swap/default menus
    if action_key in ("swap_audio", "default_audio", "default_sub"):
        remove_list = (
            state.get("remove_audio", [])
            if is_audio
            else state.get("remove_sub", [])
        )
        tracks = [t for t in tracks if t["index"] not in remove_list]

    if not tracks:
        back_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back", callback_data=f"vt_main_{task_id}"
                    )
                ]
            ]
        )
        if action_key in ("swap_audio", "default_audio", "default_sub"):
            await query.message.edit_text(
                "<b>No stream left!</b>\n\nKindly check your 'Remove Streams' selection.",
                reply_markup=back_markup,
            )
        else:
            await query.message.edit_text(
                "<b>No tracks found</b> for this type.",
                reply_markup=back_markup,
            )
        return

    markup = []

    for t in tracks:
        idx = t["index"]
        text_disp = f"Track {idx + 1} - {t['lang'].upper()} ({t.get('codec', '')})"

        if action_key in ("default_audio", "default_sub"):
            icon = "✅ " if state[action_key] == idx else "❌ "
        elif action_key == "swap_audio":
            swap_val = state["swap_audio"].get(str(idx), 0)
            icon = f"{swap_val}️⃣ " if swap_val > 0 else "❌ "
        else:
            icon = "✅ " if idx in state[action_key] else "❌ "

        btn_text = icon + text_disp
        cb_data = f"vt_toggle_{action_key}_{idx}_{task_id}"
        markup.append([InlineKeyboardButton(btn_text, callback_data=cb_data)])

    # Select All / Deselect All for remove/extract
    if action_key in ("remove_audio", "remove_sub", "extract_audio", "extract_sub"):
        is_all_selected = len(state[action_key]) == len(tracks)
        all_text = "Deselect All" if is_all_selected else "Select All"
        cb_all = f"vt_toggle_{action_key}_all_{task_id}"
        markup.append([InlineKeyboardButton(all_text, callback_data=cb_all)])

    # Back button
    if action_key in ("swap_audio", "default_audio", "default_sub"):
        back_cb = f"vt_main_{task_id}"
    else:
        root_action = action_key.split("_")[0]
        back_cb = f"vt_{root_action}_{task_id}"

    markup.append([InlineKeyboardButton("⬅️ Back", callback_data=back_cb)])

    await query.message.edit_text(
        f"<b>{title}</b>\n\n"
        f"Select the streams you wish to modify. Changes are saved automatically.",
        reply_markup=InlineKeyboardMarkup(markup),
    )


async def video_tools_callback(_, query):
    """Handle all vt_ callback queries."""
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

    # Find the listener with this task_id
    from .. import task_dict, task_dict_lock

    listener = None
    async with task_dict_lock:
        for mid, tsk in task_dict.items():
            if str(mid) == task_id and hasattr(tsk, "listener"):
                listener = tsk.listener()
                break

    # Fallback: try to get state from the event system
    event = get_vt_event(task_id)

    if event is None:
        await query.answer("Session expired or already processed!", show_alert=True)
        return

    # Get state from listener
    state = None
    async with task_dict_lock:
        for mid, tsk in task_dict.items():
            if str(mid) == task_id:
                actual_listener = getattr(tsk, "_listener", None) or getattr(
                    tsk, "listener", None
                )
                if actual_listener and callable(actual_listener):
                    actual_listener = actual_listener()
                if actual_listener and hasattr(actual_listener, "_vt_state"):
                    state = actual_listener._vt_state
                break

    if state is None:
        await query.answer("Session state not found!", show_alert=True)
        return

    if state.get("completed"):
        await query.answer("This task is already executed!", show_alert=True)
        return

    await query.answer()

    try:
        # CLOSE
        if action == "close":
            state["completed"] = True
            state["cancelled"] = True
            event.set()
            await query.message.edit_text(
                "<b>Video Tools Cancelled.</b> Proceeding normally..."
            )
            return

        # DONE
        if action == "done":
            state["completed"] = True
            event.set()
            await query.message.edit_text(
                "✅ <b>Video Tools configuration saved!</b> Processing..."
            )
            return

        # MAIN MENU
        if action == "main":
            await render_video_tools_main(query.message, state)
            return

        # REMOVE / EXTRACT submenu
        if action in ("remove", "extract"):
            action_name = (
                "Remove Stream" if action == "remove" else "Extract Stream"
            )
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🎵 Audio",
                            callback_data=f"vt_list_{action}_audio_{task_id}",
                        ),
                        InlineKeyboardButton(
                            "📝 Subtitle",
                            callback_data=f"vt_list_{action}_sub_{task_id}",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data=f"vt_main_{task_id}",
                        )
                    ],
                ]
            )
            await query.message.edit_text(
                f"<b>{action_name}</b>\n\nChoose which stream type to configure:",
                reply_markup=markup,
            )
            return

        # SWAP / DEFAULT AUDIO / DEFAULT SUB
        if action == "swap":
            await render_stream_list(query, state, "swap_audio", "Audio Swap")
            return
        if action == "defa":
            await render_stream_list(
                query, state, "default_audio", "Default Audio"
            )
            return
        if action == "defs":
            await render_stream_list(
                query, state, "default_sub", "Default Subtitle"
            )
            return

        # LIST (remove/extract audio/sub)
        if action == "list":
            mode = parts[2]
            track_type = parts[3]
            action_key = f"{mode}_{track_type}"
            await render_stream_list(
                query,
                state,
                action_key,
                f"{mode.title()} {track_type.title()}",
            )
            return

        # TOGGLE
        if action == "toggle":
            action_key = parts[2] + "_" + parts[3]
            idx_str = parts[4]

            if idx_str == "all":
                tracks = (
                    state["audio_tracks"]
                    if "audio" in action_key
                    else state["sub_tracks"]
                )
                all_indices = [t["index"] for t in tracks]
                if len(state[action_key]) == len(all_indices):
                    state[action_key] = []
                else:
                    state[action_key] = list(all_indices)
            else:
                idx = int(idx_str)
                if action_key in ("default_audio", "default_sub"):
                    if state[action_key] == idx:
                        state[action_key] = None
                    else:
                        state[action_key] = idx
                elif action_key == "swap_audio":
                    curr_val = state["swap_audio"].get(str(idx), 0)
                    if curr_val == 0:
                        max_val = (
                            max(state["swap_audio"].values())
                            if state["swap_audio"]
                            else 0
                        )
                        state["swap_audio"][str(idx)] = max_val + 1
                    else:
                        del state["swap_audio"][str(idx)]
                        sorted_swaps = sorted(
                            state["swap_audio"].items(), key=lambda x: x[1]
                        )
                        state["swap_audio"] = {
                            k: i + 1 for i, (k, v) in enumerate(sorted_swaps)
                        }
                else:
                    if idx in state[action_key]:
                        state[action_key].remove(idx)
                    else:
                        state[action_key].append(idx)
                        # If removing, clean up swap/default references
                        if action_key == "remove_audio":
                            if state.get("default_audio") == idx:
                                state["default_audio"] = None
                            if str(idx) in state.get("swap_audio", {}):
                                del state["swap_audio"][str(idx)]
                                sorted_swaps = sorted(
                                    state["swap_audio"].items(),
                                    key=lambda x: x[1],
                                )
                                state["swap_audio"] = {
                                    k: i + 1
                                    for i, (k, v) in enumerate(sorted_swaps)
                                }
                        elif action_key == "remove_sub":
                            if state.get("default_sub") == idx:
                                state["default_sub"] = None

            title_map = {
                "remove_audio": "Remove Audio",
                "remove_sub": "Remove Subtitle",
                "extract_audio": "Extract Audio",
                "extract_sub": "Extract Subtitle",
                "swap_audio": "Audio Swap",
                "default_audio": "Default Audio",
                "default_sub": "Default Subtitle",
            }
            await render_stream_list(
                query, state, action_key, title_map[action_key]
            )

    except Exception as e:
        err_msg = f"{type(e).__name__}: {e}"
        LOGGER.error(f"VT callback error: action={action} data={data} - {e}")
        try:
            await query.answer(err_msg[:200], show_alert=True)
        except Exception:
            pass
