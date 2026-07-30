import os
from asyncio import Lock as AsyncLock, create_task, sleep as asleep
from contextlib import suppress
from secrets import token_hex
from time import time

from aiofiles.os import makedirs

from .... import LOGGER, task_dict, task_dict_lock, user_data
from ....core.config_manager import Config
from ...ext_utils.mega_compat import (
    MegaApi,
    MegaCancelToken,
    MegaStringList,
    MegaSdkUnavailable,
    ensure_mega_sdk,
)
from ...telegram_helper.message_utils import send_message, send_status_message
from ...ext_utils.bot_utils import mega_selection_buttons, sync_to_async
from ...ext_utils.task_manager import (
    check_running_tasks,
    limit_checker,
    stop_duplicate_check,
)
from ...ext_utils.files_utils import clean_download
from ...ext_utils.links_utils import get_mega_subfolder_handle, is_mega_folder_link
from ...listeners.mega_listener import (
    AsyncMega,
    MegaAppListener,
    MegaFolderListener,
    _mega_error_format,
    _MEGA_SDK_LOCK,
)
from ...mirror_leech_utils.status_utils.mega_status import MegaDownloadStatus
from ...mirror_leech_utils.status_utils.queue_status import QueueStatus


_ACTIVE_MEGA_LINKS = set()
_ACTIVE_MEGA_LINKS_LOCK = AsyncLock()
_MEGA_SELECTIONS = {}
_MEGA_SELECTION_TTL = 30 * 60

from web.mega_selection_store import (
    delete_state as _delete_selection,
    read_state as _read_selection,
    write_state as _write_selection,
)

_MEGA_BASE64_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _mega_base64_to_int(handle_str: str) -> int | None:
    if not handle_str:
        return None
    try:
        val = 0
        for c in handle_str:
            idx = _MEGA_BASE64_ALPHABET.find(c)
            if idx < 0:
                return None
            val = (val << 6) | idx
        return val & ((1 << 64) - 1)
    except Exception:
        return None


def _find_child_by_handle(api, parent_node, target_handle):
    if not parent_node or not target_handle:
        return None
    try:
        children = api.getChildren(parent_node)
        return _find_child_in_list(children, target_handle)
    except Exception as e:
        LOGGER.warning(f"_find_child_by_handle error: {e}")
    return None


def _find_child_in_list(children, target_handle):
    if not children:
        return None
    try:
        _to_handle = getattr(MegaApi, "base64ToHandle", None)
        target_int = _to_handle(target_handle) if callable(_to_handle) else None
    except Exception:
        target_int = None
    for i in range(children.size()):
        child = children.get(i)
        try:
            ch = child.getHandle()
            if ch == target_handle or (target_int is not None and ch == target_int):
                return child
        except Exception:
            pass
    return None


def _make_cancel_token():
    if MegaCancelToken is None:
        return None
    try:
        return MegaCancelToken.createInstance()
    except Exception as e:
        LOGGER.error(f"Mega: failed to create cancel token: {e}")
        return None


async def _reserve_link(link: str):
    async with _ACTIVE_MEGA_LINKS_LOCK:
        if link in _ACTIVE_MEGA_LINKS:
            return False
        _ACTIVE_MEGA_LINKS.add(link)
        return True


async def _release_link(link: str):
    async with _ACTIVE_MEGA_LINKS_LOCK:
        _ACTIVE_MEGA_LINKS.discard(link)


def _safe_node_name(name):
    return "".join("_" if char in '/\\\0:*?\"<>|' else char for char in str(name or "unnamed")).strip() or "unnamed"


def _walk_mega_tree(api, root):
    entries = []
    stack = [(root, "")]
    while stack:
        parent, prefix = stack.pop()
        children = api.getChildren(parent)
        if not children:
            continue
        local = [children.get(index) for index in range(children.size())]
        for child in local:
            name = _safe_node_name(child.getName())
            is_dir = bool(child.isFolder()) or child.getType() == 1
            handle = str(child.getHandle())
            handle_b64 = MegaApi.handleToBase64(child.getHandle())
            entries.append(
                {
                    "name": name,
                    "size": 0 if is_dir else max(0, int(child.getSize() or 0)),
                    "path": prefix,
                    "id": handle,
                    "handle_b64": handle_b64,
                    "is_dir": is_dir,
                }
            )
            if is_dir:
                stack.append((child, f"{prefix}{name}/"))
    return entries


def get_mega_selection_owner_id(gid):
    state = _MEGA_SELECTIONS.get(gid)
    return getattr(state.get("listener"), "user_id", None) if state else None


async def _close_selection_state(state):
    if not state:
        return
    async_api = state.get("async_api")
    if async_api is not None:
        async with _MEGA_SDK_LOCK:
            with suppress(Exception):
                await async_api.logout()
            folder_api = state.get("folder_api")
            folder_listener = state.get("dl_listener")
            if folder_api is not None and folder_listener is not None:
                with suppress(Exception):
                    folder_api.removeListener(folder_listener)
            api = getattr(async_api, "api", None)
            mega_listener = getattr(async_api, "_mega_listener", None)
            if api is not None and mega_listener is not None:
                with suppress(Exception):
                    api.removeListener(mega_listener)
    await _release_link(state["listener"].link)
    await clean_download(state.get("mega_base", ""))


async def _expire_mega_selection(gid):
    await asleep(_MEGA_SELECTION_TTL)
    state = _MEGA_SELECTIONS.pop(gid, None)
    if state is None:
        return
    _delete_selection(gid)
    listener = state["listener"]
    if not listener.is_cancelled:
        listener.is_cancelled = True
        await listener.on_download_error("MEGA file selection timed out")
    await _close_selection_state(state)


async def cancel_mega_selection(gid):
    state = _MEGA_SELECTIONS.pop(gid, None)
    _delete_selection(gid)
    if state:
        state["listener"].is_cancelled = True
        await state["listener"].on_download_error("MEGA selection cancelled by user")
        await _close_selection_state(state)


async def resume_mega_with_selection(gid):
    state = _MEGA_SELECTIONS.pop(gid, None)
    stored = _read_selection(gid)
    _delete_selection(gid)
    if not state or not stored:
        return False
    listener = state["listener"]
    api = state["folder_api"]
    async_api = state["async_api"]
    dl_listener = state["dl_listener"]
    selected = set(stored.get("selected_ids") or [])
    entries = [item for item in state["entries"] if not item["is_dir"] and item["id"] in selected]
    try:
        if not entries:
            await listener.on_download_error("No MEGA files selected")
            return False
        listener.size = sum(item["size"] for item in entries)
        msg, button = await stop_duplicate_check(listener)
        if msg:
            await listener.on_download_error(msg, button)
            return False
        if error := await limit_checker(listener):
            await listener.on_download_error(error, is_limit=True)
            return False
        filters = MegaStringList.createInstance()
        if filters is None:
            await listener.on_download_error("This MEGA SDK build does not support folder selection")
            return False
        for item in entries:
            filters.add(item["handle_b64"])
        await sync_to_async(api.setFolderDownloadFilter, filters)
        dl_listener._caller_manages_completion = True
        dl_listener._cancel_token = _make_cancel_token()
        dl_listener._selection_cleanup = None
        async_api._download_is_folder = True
        added_to_queue, event = await check_running_tasks(listener)
        if added_to_queue:
            async with task_dict_lock:
                task_dict[listener.mid] = QueueStatus(listener, gid, "dl")
            await listener.on_download_start()
            if listener.multi <= 1:
                await send_status_message(listener.message)
            await event.wait()
            if listener.is_cancelled:
                return False
        async with task_dict_lock:
            task_dict[listener.mid] = MegaDownloadStatus(listener, dl_listener, gid, "dl")
        if not added_to_queue:
            await listener.on_download_start()
            if listener.multi <= 1:
                await send_status_message(listener.message)
        download_path = os.path.join(state["path"], listener.name)
        await makedirs(download_path, exist_ok=True)
        await async_api.startDownload(
            state["node"], download_path, listener.name, None, False,
            dl_listener._cancel_token, 3, 2, False,
        )
        await async_api.wait_for_transfer()
        with suppress(Exception):
            await sync_to_async(api.clearFolderDownloadFilter)
        if not listener.is_cancelled and not dl_listener.is_cancelled:
            if dl_listener.error:
                await listener.on_download_error(_mega_error_format(dl_listener.error))
            else:
                await listener.on_download_complete()
        return True
    except Exception as error:
        LOGGER.error("MEGA selected download failed: %s", error, exc_info=True)
        if not listener.is_cancelled:
            await listener.on_download_error(f"MEGA selected download failed: {error}")
        return False
    finally:
        await _close_selection_state(state)


async def add_mega_download(listener, path):
    if Config.DISABLE_MEGA:
        await listener.on_download_error(
            "Mega Link downloads are currently disabled by the Bot Owner."
        )
        return
    handoff = False
    try:
        ensure_mega_sdk()
    except MegaSdkUnavailable as e:
        await listener.on_download_error(str(e))
        return

    user_dict = user_data.get(listener.user_id, {})
    mega_email = user_dict.get("MEGA_EMAIL") or Config.MEGA_EMAIL
    mega_password = user_dict.get("MEGA_PASSWORD") or Config.MEGA_PASSWORD

    if not await _reserve_link(listener.link):
        await listener.on_download_error(
            "This Mega link is already being downloaded! Wait for it to finish."
        )
        return

    async_api = None
    mega_base = ""
    try:
        sdk_gid = token_hex(5)
        await makedirs(path, exist_ok=True)
        mega_base = os.path.join(
            os.path.dirname(path.rstrip("/")), ".mega_sdk", sdk_gid
        )
        mega_dir = os.path.join(mega_base, "main")
        await makedirs(mega_dir, exist_ok=True)

        async_api = AsyncMega()
        async_api.api = api = MegaApi("", mega_dir, "WZML-X", 4)
        mega_listener = MegaAppListener(async_api, listener)
        async_api._mega_listener = mega_listener
        api.addListener(mega_listener)
        api._listener_ref = mega_listener

        is_folder = is_mega_folder_link(listener.link)
        subfolder_handle = get_mega_subfolder_handle(listener.link)

        if is_folder:
            async_api.folder_api = folder_api = MegaApi("", mega_dir, "WZML-X", 4)
            folder_listener = MegaFolderListener(async_api, listener)
            async_api._folder_listener = folder_listener
            folder_api.addListener(folder_listener)
            folder_api._listener_ref = folder_listener
            dl_listener = folder_listener

            await async_api.loginToFolder(listener.link)
            if listener.is_cancelled or dl_listener.is_cancelled:
                return
            if dl_listener.error:
                await listener.on_download_error(_mega_error_format(dl_listener.error))
                return
            await async_api.fetchNodes(api=folder_api)
            await asleep(0)
            if listener.is_cancelled or dl_listener.is_cancelled:
                LOGGER.info("Mega: cancelled after fetchNodes")
                return
            if dl_listener.error:
                LOGGER.info("Mega: error after fetchNodes: %s", dl_listener.error)
                await listener.on_download_error(_mega_error_format(dl_listener.error))
                return
            if not dl_listener.node:
                LOGGER.info("Mega: no root node after fetchNodes")
                await listener.on_download_error(
                    "Failed to get root node for MEGA folder"
                )
                return
            if subfolder_handle:
                LOGGER.info("Mega: looking up subfolder handle=%s", subfolder_handle)
                target_int = _mega_base64_to_int(subfolder_handle)
                node = _find_child_in_list(dl_listener._children, subfolder_handle)
                if not node and target_int is not None:
                    try:
                        node = folder_api.getNodeByHandle(target_int)
                    except Exception as e:
                        LOGGER.error("Mega: getNodeByHandle failed: %s", e)
                if not node:
                    await listener.on_download_error(
                        "Subfolder not found in the MEGA link"
                    )
                    return
                dl_listener.node = node
                dl_listener._cache_node_data(node)
                LOGGER.info("Mega: subfolder name=%s", dl_listener._name)

                dl_listener._size = listener.size
                if not dl_listener._size:
                    try:
                        s = node.getSize()
                        dl_listener._size = s if s < (1 << 62) else -1
                    except Exception:
                        pass
                LOGGER.info("Mega: subfolder size=%s", dl_listener._size)
            else:
                node = dl_listener.node
        else:
            dl_listener = mega_listener
            if mega_email and mega_password:
                await async_api.login(mega_email, mega_password)
                if listener.is_cancelled or mega_listener.is_cancelled:
                    return
                if mega_listener.error:
                    await listener.on_download_error(
                        _mega_error_format(mega_listener.error)
                    )
                    return
                await async_api.fetchNodes()
                if listener.is_cancelled or mega_listener.is_cancelled:
                    return
                if mega_listener.error:
                    await listener.on_download_error(
                        _mega_error_format(mega_listener.error)
                    )
                    return
            await async_api.getPublicNode(listener.link)
            if listener.is_cancelled or mega_listener.is_cancelled:
                return
            node = mega_listener.public_node
            if not node:
                await listener.on_download_error("Failed to resolve MEGA link")
                return

        listener.name = (
            listener.name or dl_listener._name or f"MEGA_Download_{token_hex(5)}"
        )
        listener.size = dl_listener._size if dl_listener._size < (1 << 62) else -1
        if listener.size <= 0 and node:
            try:
                s = node.getSize()
                listener.size = s if s < (1 << 62) else -1
            except Exception:
                pass
        gid = token_hex(5)

        if listener.select and is_folder:
            if not Config.BASE_URL:
                await listener.on_download_error("BASE_URL is required for MEGA file selection")
                return
            entries = await sync_to_async(_walk_mega_tree, folder_api, node)
            if not any(not item["is_dir"] for item in entries):
                await listener.on_download_error("MEGA folder contains no files")
                return
            public_gid = f"mega_{gid}"
            metadata = entries
            if not _write_selection(gid, metadata, []):
                await listener.on_download_error("Failed to create MEGA selection state")
                return
            _MEGA_SELECTIONS[gid] = {
                "listener": listener,
                "async_api": async_api,
                "folder_api": folder_api,
                "dl_listener": dl_listener,
                "entries": entries,
                "node": node,
                "path": path,
                "mega_base": mega_base,
                "created_at": time(),
            }
            dl_listener._selection_cleanup = lambda: cancel_mega_selection(gid)
            create_task(_expire_mega_selection(gid))
            listener.size = sum(item["size"] for item in entries if not item["is_dir"])
            async with task_dict_lock:
                task_dict[listener.mid] = MegaDownloadStatus(listener, dl_listener, public_gid, "dl")
            await send_message(
                listener.message,
                "<b>MEGA folder ready.</b> Select files, submit the page, then press Done Selecting.",
                mega_selection_buttons(public_gid),
            )
            handoff = True
            return
        msg, button = await stop_duplicate_check(listener)
        if msg:
            await listener.on_download_error(msg, button)
            return

        if limit_exceeded := await limit_checker(listener):
            await listener.on_download_error(limit_exceeded, is_limit=True)
            return

        added_to_queue, event = await check_running_tasks(listener)
        if added_to_queue:
            async with task_dict_lock:
                task_dict[listener.mid] = QueueStatus(listener, gid, "dl")
            await listener.on_download_start()
            if listener.multi <= 1:
                await send_status_message(listener.message)
            await event.wait()
            if listener.is_cancelled:
                return

        async with task_dict_lock:
            task_dict[listener.mid] = MegaDownloadStatus(
                listener, dl_listener, gid, "dl"
            )

        if added_to_queue:
            await listener.on_download_start()
        else:
            await listener.on_download_start()
            if listener.multi <= 1:
                await send_status_message(listener.message)

        if listener.is_cancelled or dl_listener.is_cancelled:
            return
        download_path = path
        if is_mega_folder_link(listener.link):
            download_path = os.path.join(path, listener.name)
            await makedirs(download_path, exist_ok=True)

        for attempt in range(5):
            cancel_token = _make_cancel_token()
            dl_listener._cancel_token = cancel_token
            dl_listener.error = None
            dl_listener.retryable_error = None
            dl_listener._bytes_transferred = 0
            dl_listener._total_downloaded_bytes = 0
            dl_listener._caller_manages_completion = False

            await async_api.startDownload(
                node,
                download_path,
                listener.name,
                None,
                False,
                cancel_token,
                3,
                2,
                False,
            )
            await async_api.wait_for_transfer()

            if listener.is_cancelled or dl_listener.is_cancelled:
                return
            if not dl_listener.retryable_error:
                return
            if attempt >= 4:
                await listener.on_download_error(
                    _mega_error_format(dl_listener.retryable_error)
                )
                return
            await clean_download(download_path)
            await asleep(2**attempt)

    except Exception as e:
        LOGGER.error(f"Unexpected error in add_mega_download: {e}", exc_info=True)
        if not listener.is_cancelled:
            await listener.on_download_error(f"Internal error: {e}")
    finally:
        if async_api is not None:
            if not is_folder:
                async with _MEGA_SDK_LOCK:
                    with suppress(Exception):
                        await async_api.logout()
                    if (
                        async_api.api is not None
                        and async_api._mega_listener is not None
                    ):
                        with suppress(Exception):
                            async_api.api.removeListener(async_api._mega_listener)
                    if (
                        async_api.folder_api is not None
                        and async_api._folder_listener is not None
                    ):
                        with suppress(Exception):
                            async_api.folder_api.removeListener(
                                async_api._folder_listener
                            )
        if not handoff:
            await _release_link(listener.link)
            await clean_download(mega_base)
