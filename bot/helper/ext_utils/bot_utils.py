from asyncio import (
    create_subprocess_exec,
    create_subprocess_shell,
    run_coroutine_threadsafe,
    sleep,
)
from asyncio.subprocess import PIPE
from concurrent.futures import ThreadPoolExecutor
from functools import partial, wraps
from hashlib import sha256
from hmac import new as hmac_new
from secrets import token_bytes

from httpx import AsyncClient
from pyrogram.handlers import MessageHandler

from ... import LOGGER, bot_loop, user_data
from ...core.config_manager import Config
from .db_handler import database
from ..telegram_helper.button_build import ButtonMaker
from .help_messages import (
    CLONE_HELP_DICT,
    MIRROR_HELP_DICT,
    YT_HELP_DICT,
)
from .telegraph_helper import telegraph

COMMAND_USAGE = {}

THREAD_POOL = ThreadPoolExecutor(max_workers=500)
_SERVICE_PWD_SALT = b"wzmlx_v3_service_pwd_salt"
_cached_secret_bytes = None


def _shared_secret():
    global _cached_secret_bytes
    secret = Config.WEB_ACCESS_PASSWORD
    if not secret:
        if _cached_secret_bytes is None:
            _cached_secret_bytes = token_bytes(32)
        return _cached_secret_bytes
    return secret.encode("utf-8") if isinstance(secret, str) else secret


def derive_service_password(bot_id, service):
    if not bot_id:
        bot_id = "0"
    digest = hmac_new(
        _SERVICE_PWD_SALT,
        f"{bot_id}:{service}".encode("utf-8"),
        sha256,
    )
    digest.update(_shared_secret())
    raw = digest.hexdigest()
    return raw[:20] + raw[-4:]


class SetInterval:
    def __init__(self, interval, action, *args, **kwargs):
        self.interval = interval
        self.action = action
        self.task = bot_loop.create_task(self._set_interval(*args, **kwargs))

    async def _set_interval(self, *args, **kwargs):
        while True:
            await sleep(self.interval)
            await self.action(*args, **kwargs)

    def cancel(self):
        self.task.cancel()


def _build_command_usage(help_dict, command_key):
    buttons = ButtonMaker()
    cmd_list = list(help_dict.keys())[1:]
    temp_store = []
    cmd_pages = [cmd_list[i : i + 10] for i in range(0, len(cmd_list), 10)]
    for i in range(1, len(cmd_pages) + 1):
        for name in cmd_pages[i]:
            buttons.data_button(name, f"help {command_key} {name}")
        buttons.data_button("Prev", f"help pre {command_key} {i - 1}")
        buttons.data_button("Next", f"help nex {command_key} {i + 1}")
        buttons.data_button("Close", "help close", "footer")
        temp_store.append(buttons.build_menu(2))
    COMMAND_USAGE[command_key] = [help_dict["main"], *temp_store]
    buttons.reset()


def _build_command_usage(help_dict, command_key):
    buttons = ButtonMaker()
    cmd_list = list(help_dict.keys())[1:]
    cmd_pages = [cmd_list[i : i + 10] for i in range(0, len(cmd_list), 10)]
    temp_store = []

    for i, page in enumerate(cmd_pages):
        for name in page:
            buttons.data_button(name, f"help {command_key} {name} {i}")
        if len(cmd_pages) > 1:
            if i > 0:
                buttons.data_button("⫷", f"help pre {command_key} {i - 1}")
            if i < len(cmd_pages) - 1:
                buttons.data_button("⫸", f"help nex {command_key} {i + 1}")
        buttons.data_button("Close", "help close", "footer")
        temp_store.append(buttons.build_menu(2))
        buttons.reset()

    COMMAND_USAGE[command_key] = [help_dict["main"], *temp_store]


def create_help_buttons():
    _build_command_usage(MIRROR_HELP_DICT, "mirror")
    _build_command_usage(YT_HELP_DICT, "yt")
    _build_command_usage(CLONE_HELP_DICT, "clone")


def compare_versions(v1, v2):
    v1, v2 = (list(map(int, v.split("-")[0][1:].split("."))) for v in (v1, v2))
    return (
        "New Version Update is Available! Check Now!"
        if v1 < v2
        else (
            "More Updated! Kindly Contribute in Official"
            if v1 > v2
            else "Already up to date with latest version"
        )
    )


def bt_selection_buttons(id_):
    gid = id_[:12] if len(id_) > 25 else id_
    pin = "".join([n for n in id_ if n.isdigit()][:4])
    buttons = ButtonMaker()
    if Config.WEB_PINCODE:
        buttons.url_button("Select Files", f"{Config.BASE_URL}/app/files?gid={id_}")
        buttons.data_button("Pincode", f"sel pin {gid} {pin}")
    else:
        buttons.url_button(
            "Select Files", f"{Config.BASE_URL}/app/files?gid={id_}&pin={pin}"
        )
    buttons.data_button("Done Selecting", f"sel done {gid} {id_}")
    buttons.data_button("Cancel", f"sel cancel {gid}")
    return buttons.build_menu(2)


async def get_telegraph_list(telegraph_content):
    path = [
        (
            await telegraph.create_page(
                title="Mirror-Leech-Bot Drive Search", content=content
            )
        )["path"]
        for content in telegraph_content
    ]
    if len(path) > 1:
        await telegraph.edit_telegraph(path, telegraph_content)
    buttons = ButtonMaker()
    buttons.url_button("🔎 VIEW", f"https://telegra.ph/{path[0]}")
    return buttons.build_menu(1)


def arg_parser(items, arg_base):
    if not items:
        return

    arg_start = -1
    i = 0
    total = len(items)

    bool_arg_set = {
        "-b",
        "-e",
        "-z",
        "-s",
        "-j",
        "-d",
        "-sv",
        "-ss",
        "-f",
        "-fd",
        "-fu",
        "-sync",
        "-hl",
        "-doc",
        "-med",
        "-ut",
        "-bt",
        "-yt",
        "-vt",
    }
    if Config.DISABLE_BULK and "-b" in items:
        arg_base["-b"] = False

    if Config.DISABLE_MULTI and "-i" in items:
        arg_base["-i"] = 0

    if Config.DISABLE_SEED and "-d" in items:
        arg_base["-d"] = False

    while i < total:
        part = items[i]

        if part in arg_base:
            if arg_start == -1:
                arg_start = i

            if (
                i + 1 == total
                and part in bool_arg_set
                or part
                in [
                    "-s",
                    "-j",
                    "-f",
                    "-fd",
                    "-fu",
                    "-sync",
                    "-hl",
                    "-doc",
                    "-med",
                    "-ut",
                    "-bt",
                    "-yt",
                    "-vt",
                ]
            ):
                arg_base[part] = True
            else:
                sub_list = []
                for j in range(i + 1, total):
                    if items[j] in arg_base:
                        if part == "-c" and items[j] == "-c":
                            sub_list.append(items[j])
                            continue
                        if part in bool_arg_set and not sub_list:
                            arg_base[part] = True
                            break
                        if not sub_list:
                            break
                        check = " ".join(sub_list).strip()
                        if check.startswith("[") and check.endswith("]"):
                            break
                        elif not check.startswith("["):
                            break
                    sub_list.append(items[j])
                if sub_list:
                    value = " ".join(sub_list)
                    if part == "-ff" and not value.strip().startswith("["):
                        arg_base[part].add(value)
                    else:
                        arg_base[part] = value
                    i += len(sub_list)

        i += 1

    if "link" in arg_base:
        link_items = items[:arg_start] if arg_start != -1 else items
        if link_items:
            arg_base["link"] = " ".join(link_items)


def get_size_bytes(size):
    size = size.lower()
    if "k" in size:
        size = int(float(size.split("k")[0]) * 1024)
    elif "m" in size:
        size = int(float(size.split("m")[0]) * 1048576)
    elif "g" in size:
        size = int(float(size.split("g")[0]) * 1073741824)
    elif "t" in size:
        size = int(float(size.split("t")[0]) * 1099511627776)
    else:
        size = 0
    return size


def handleIndex(index, lst):
    if not lst:
        return 0
    return index % len(lst)


def fetch_drive_cat(user_id, force=False):
    user_dict = user_data.get(user_id, {})
    if (Config.DRIVE_CATEGORY_MODE and user_dict.get("drive_cat_mode", False)) or force:
        return user_dict.get("DRIVE_CAT", {})
    return {}


async def get_content_type(url):
    try:
        async with AsyncClient() as client:
            response = await client.get(url, allow_redirects=True, verify=False)
            return response.headers.get("Content-Type")
    except Exception:
        return None


def update_user_ldata(id_, key, value):
    user_data.setdefault(id_, {})
    user_data[id_][key] = value


async def cmd_exec(cmd, shell=False):
    if shell:
        proc = await create_subprocess_shell(cmd, stdout=PIPE, stderr=PIPE)
    else:
        proc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await proc.communicate()
    try:
        stdout = stdout.decode().strip()
    except Exception:
        stdout = "Unable to decode the response!"
    try:
        stderr = stderr.decode().strip()
    except Exception:
        stderr = "Unable to decode the error!"
    return stdout, stderr, proc.returncode


def new_task(func):
    async def _notify_error(update, error):
        try:
            from ..telegram_helper.message_utils import send_message

            target = getattr(update, "message", None) or update
            if target is not None:
                await send_message(
                    target,
                    f"<b>Command failed:</b>\n<code>{str(error)[:1000]}</code>",
                )
        except Exception:
            LOGGER.error("Failed to notify command error", exc_info=True)

    def _log_task_result(task, update):
        if task.cancelled():
            return
        try:
            error = task.exception()
        except Exception as e:
            error = e
        if error is None:
            return
        LOGGER.error(
            f"Command task failed in {func.__name__}: {error}",
            exc_info=(type(error), error, error.__traceback__),
        )
        bot_loop.create_task(_notify_error(update, error))

    @wraps(func)
    async def wrapper(*args, **kwargs):
        task = bot_loop.create_task(func(*args, **kwargs))
        update = args[1] if len(args) > 1 else None
        task.add_done_callback(lambda done: _log_task_result(done, update))
        return task

    return wrapper


async def sync_to_async(func, *args, wait=True, **kwargs):
    pfunc = partial(func, *args, **kwargs)
    future = bot_loop.run_in_executor(THREAD_POOL, pfunc)
    return await future if wait else future


def async_to_sync(func, *args, wait=True, **kwargs):
    future = run_coroutine_threadsafe(func(*args, **kwargs), bot_loop)
    return future.result() if wait else future


def loop_thread(func):
    @wraps(func)
    def wrapper(*args, wait=False, **kwargs):
        future = run_coroutine_threadsafe(func(*args, **kwargs), bot_loop)
        return future.result() if wait else future

    return wrapper


def safe_int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


async def search_images():
    if not Config.USE_IMAGES:
        return
    try:
        if Config.DATABASE_URL:
            await database.update_config({"IMAGES": Config.IMAGES})
    except Exception as e:
        LOGGER.warning(f"search_images skipped: {e}")


def _find_command_filters(flt):
    if hasattr(flt, "commands"):
        yield flt
    for attr in ("base", "other"):
        if child := getattr(flt, attr, None):
            yield from _find_command_filters(child)


def _build_command_map():
    from ...core.tg_client import TgClient

    mapping = {}
    for group in TgClient.bot.dispatcher.groups.values():
        for handler in group:
            if not isinstance(handler, MessageHandler) or handler.filters is None:
                continue
            for cmd_filter in _find_command_filters(handler.filters):
                for cmd in cmd_filter.commands:
                    mapping[cmd] = handler.callback
    return mapping


def resolve_command(command_str):
    cmd_name = command_str.strip().lstrip("/").split(maxsplit=1)[0]
    mapping = _build_command_map()
    handler = mapping.get(cmd_name)
    if handler is None and Config.CMD_SUFFIX:
        handler = mapping.get(cmd_name + Config.CMD_SUFFIX)
    if handler is None:
        LOGGER.warning(f"Unknown command '{cmd_name}' (from '{command_str}')")
    return handler
