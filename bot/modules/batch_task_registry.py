from contextlib import suppress
import json
from os import path as ospath
from secrets import token_hex

from aiofiles import open as aiopen
from aiofiles.os import makedirs

from .. import DOWNLOAD_DIR, task_dict, task_dict_lock
from ..core.config_manager import Config
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import send_message

_batch_controllers = {}
_batch_state_path = ospath.join(DOWNLOAD_DIR, "batch_task_plans.json")


def _safe_int(value, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def get_batch_limits(kind):
    if kind == "bqleech":
        return (
            _safe_int(getattr(Config, "BQLEECH_MAX_ACTIVE_DOWNLOADS", 1), 1),
            _safe_int(getattr(Config, "BQLEECH_MAX_ACTIVE_UPLOADS", 1), 1),
        )
    return (
        _safe_int(getattr(Config, "BLEECH_MAX_ACTIVE_DOWNLOADS", 1), 1),
        _safe_int(getattr(Config, "BLEECH_MAX_ACTIVE_UPLOADS", 2), 2),
    )


class BatchTaskController:
    def __init__(self, kind, message):
        self.kind = kind
        self.message = message
        self.gid = token_hex(6)
        self.user_id = (message.from_user or message.sender_chat).id
        self.cancelled = False
        self.cancel_reason = ""
        self.listeners = set()
        _batch_controllers[self.gid] = self

    def register(self, listener):
        self.listeners.add(listener)
        listener.batch_controller_gid = self.gid

    async def cancel(self, reason="cancelled"):
        self.cancelled = True
        self.cancel_reason = reason
        for listener in list(self.listeners):
            listener.is_cancelled = True
            with suppress(Exception):
                event = getattr(listener, "batch_download_event", None)
                if event and not event.is_set():
                    event.set()
            with suppress(Exception):
                event = getattr(listener, "bq_done_event", None)
                if event and not event.is_set():
                    event.set()
            async with task_dict_lock:
                task = task_dict.get(listener.mid)
            if task:
                with suppress(Exception):
                    await task.task().cancel_task()

    def close(self):
        _batch_controllers.pop(self.gid, None)


async def _read_state():
    try:
        async with aiopen(_batch_state_path, "r", encoding="utf-8") as f:
            return json.loads(await f.read() or "{}")
    except Exception:
        return {}


async def _write_state(state):
    await makedirs(DOWNLOAD_DIR, exist_ok=True)
    async with aiopen(_batch_state_path, "w", encoding="utf-8") as f:
        await f.write(json.dumps(state, indent=2, ensure_ascii=False, default=str))


async def save_batch_plan(controller, payload):
    state = await _read_state()
    state[controller.gid] = {
        "kind": controller.kind,
        "owner": controller.user_id,
        "chat_id": controller.message.chat.id,
        "message_id": controller.message.id,
        "current_index": 0,
        "cancelled": False,
        **(payload or {}),
    }
    await _write_state(state)


async def update_batch_plan(gid, **updates):
    state = await _read_state()
    if gid in state:
        state[gid].update(updates)
        await _write_state(state)


async def finish_batch_plan(gid, cancelled=False):
    state = await _read_state()
    if gid in state:
        if cancelled:
            state[gid]["cancelled"] = True
            state[gid]["finished"] = False
        else:
            state.pop(gid, None)
        await _write_state(state)


async def load_unfinished_batch_plans():
    state = await _read_state()
    return {
        gid: plan
        for gid, plan in state.items()
        if not plan.get("cancelled") and not plan.get("finished")
    }


def mark_controller_cancelled(gid, reason):
    controller = _batch_controllers.get(gid)
    if controller:
        controller.cancelled = True
        controller.cancel_reason = reason


async def cancel_batch_controller(gid, user_id, is_sudo=False):
    controller = _batch_controllers.get(gid)
    if not controller:
        return None
    if not is_sudo and user_id != controller.user_id and user_id != Config.OWNER_ID:
        return False
    await controller.cancel("cancelled by user")
    await finish_batch_plan(gid, cancelled=True)
    return True


async def resume_unfinished_batch_plans(client):
    if not getattr(Config, "BATCH_TASK_RESTART_RESUME", True):
        return

    plans = await load_unfinished_batch_plans()
    if not plans:
        return

    from .. import LOGGER

    LOGGER.info(f"Batch restart recovery: found {len(plans)} saved plan(s)")
    for gid, plan in plans.items():
        kind = plan.get("kind")
        chat_id = plan.get("chat_id")
        message_id = plan.get("message_id")
        try:
            message = await client.get_messages(chat_id=chat_id, message_ids=message_id)
            if not message:
                await finish_batch_plan(gid, cancelled=True)
                LOGGER.warning(f"Batch restart recovery: missing message for {gid}")
                continue

            if kind == "bleech":
                links = list(plan.get("links") or [])
                index = int(plan.get("current_index") or 0)
                remaining = links[index:]
                if not remaining:
                    await finish_batch_plan(gid)
                    continue
                message.text = f"/{BotCommands.BatchLeechCommand[0]} {' '.join(remaining)}"
                await send_message(
                    message,
                    (
                        "<b>Batch Leech restart recovery</b>\n"
                        f"Old controller: <code>{gid}</code>\n"
                        f"Resuming from link <code>{index + 1}</code>."
                    ),
                )
                await finish_batch_plan(gid)
                from .batch_leech import batch_leech

                await batch_leech(client, message)
            elif kind == "bqleech":
                source = plan.get("source")
                if not source:
                    await finish_batch_plan(gid, cancelled=True)
                    continue
                resume_index = int(plan.get("current_index") or 0)
                message.text = f"/{BotCommands.BigQLeechCommand[0]} {source}"
                message.bq_resume_index = resume_index
                await send_message(
                    message,
                    (
                        "<b>Big Queue Leech restart recovery</b>\n"
                        f"Old controller: <code>{gid}</code>\n"
                        f"Re-planning source from batch <code>{resume_index + 1}</code>."
                    ),
                )
                await finish_batch_plan(gid)
                from .big_queue_leech import bq_leech

                await bq_leech(client, message)
            else:
                await finish_batch_plan(gid, cancelled=True)
                LOGGER.warning(f"Batch restart recovery: unknown plan kind {kind}")
        except Exception as e:
            LOGGER.error(f"Batch restart recovery failed for {gid}: {e}", exc_info=True)
