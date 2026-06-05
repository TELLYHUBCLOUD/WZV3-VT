import contextlib

from asyncio import Lock, sleep
from dataclasses import dataclass
from hashlib import sha256
from time import time

from pyrogram.errors import FloodWait

try:
    from pyrogram.errors import FloodPremiumWait
except ImportError:
    FloodPremiumWait = FloodWait

from ... import LOGGER, sudo_users, user_data
from ...core.config_manager import Config
from ...core.tg_client import TgClient
from .bot_utils import update_user_ldata
from .db_handler import database
from .performance import resources_overloaded

BOT_UPLOAD_LIMIT = 2097152000

USER_BOT_TOKEN_KEY = "USER_UPLOAD_BOT_TOKEN"
USER_BOT_META_KEY = "USER_UPLOAD_BOT_META"

HELPER_TOKENS_KEY = "HELPER_TOKEN_LIST"
HELPER_PIN_HASH_KEY = "HELPER_TOKEN_PIN_HASH"


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def mask_token(token):
    if not token or ":" not in str(token):
        return "Not Set"
    left, right = str(token).split(":", 1)
    return f"{left}:****{right[-4:] if len(right) >= 4 else '****'}"


def token_hash(token):
    return sha256(str(token).encode()).hexdigest()


def _pin_hash(user_id, pin):
    bot_seed = str(getattr(Config, "BOT_TOKEN", "") or "").split(":", 1)[0]
    return sha256(f"{bot_seed}:{user_id}:{pin}".encode()).hexdigest()


def parse_dump_chat():
    dump = str(Config.LEECH_DUMP_CHAT or "").strip()
    if not dump:
        return None, None
    thread_id = None
    if "|" in dump:
        dump, thread = dump.split("|", 1)
        thread_id = int(thread) if thread.lstrip("-").isdigit() else None
    if dump.lstrip("-").isdigit():
        dump = int(dump)
    elif dump.lower() == "pm":
        dump = Config.OWNER_ID
    return dump, thread_id


def is_sudo_user(user_id):
    return bool(
        user_id == Config.OWNER_ID
        or user_id in sudo_users
        or user_data.get(user_id, {}).get("SUDO")
    )


def is_blacklisted(token=None, bot_id=None, username=None):
    entries = {
        item.strip().lower().lstrip("@")
        for item in str(Config.UPLOAD_BOT_TOKEN_BLACKLIST or "").split()
        if item.strip()
    }
    if not entries:
        return False
    checks = set()
    if token:
        checks.add(str(token).strip().lower())
        checks.add(str(token).split(":", 1)[0].lower())
        checks.add(token_hash(token).lower())
    if bot_id:
        checks.add(str(bot_id).lower())
    if username:
        checks.add(str(username).strip().lower().lstrip("@"))
        checks.add(f"@{str(username).strip().lower().lstrip('@')}")
    return any(item in entries for item in checks)


def private_dump_only(text):
    haystack = str(text or "").lower()
    keywords = [
        item.strip().lower()
        for item in str(Config.UPLOAD_PRIVATE_DUMP_ONLY_KEYWORDS or "").split()
        if item.strip()
    ]
    domains = [
        item.strip().lower()
        for item in str(Config.UPLOAD_PRIVATE_DUMP_ONLY_DOMAINS or "").split()
        if item.strip()
    ]
    return any(word in haystack for word in keywords + domains)


def _error_status(error):
    text = str(error).lower()
    if any(
        key in text
        for key in (
            "chat_write_forbidden",
            "channel_private",
            "peer_id_invalid",
            "not a member",
            "forbidden",
        )
    ):
        return "Not In Dump"
    return "Failed"


def _normalize_record(record, primary=False):
    token = str(record.get("token", "") or "").strip()
    return {
        "token": token,
        "token_hash": record.get("token_hash") or token_hash(token),
        "mask": mask_token(token),
        "bot_id": record.get("bot_id", ""),
        "username": record.get("username", "Not Tested") or "Not Tested",
        "status": record.get("status", "Not Tested") or "Not Tested",
        "primary": bool(record.get("primary", primary)),
        "updated_at": int(record.get("updated_at", 0) or 0),
        "last_error": record.get("last_error", ""),
    }


def _safe_record(record, runtime_status=None):
    safe = {
        key: value
        for key, value in record.items()
        if key not in {"token"}
    }
    safe["token"] = mask_token(record.get("token"))
    safe["mask"] = mask_token(record.get("token"))
    if runtime_status:
        safe["status"] = runtime_status
    return safe


def safe_helper_tokens(user_dict):
    records = user_dict.get(HELPER_TOKENS_KEY, []) or []
    if not isinstance(records, list):
        records = []
    return [_safe_record(_normalize_record(record)) for record in records if record.get("token")]


def safe_user_token_data(user_dict):
    records = safe_helper_tokens(user_dict)
    if records:
        primary = next((item for item in records if item.get("primary")), records[0])
        return {
            "token": primary.get("token"),
            "username": primary.get("username", "Not Tested"),
            "bot_id": primary.get("bot_id", ""),
            "status": primary.get("status", "Not Tested"),
            "last_error": primary.get("last_error", ""),
            "count": len(records),
        }
    token = user_dict.get(USER_BOT_TOKEN_KEY, "")
    meta = user_dict.get(USER_BOT_META_KEY, {}) or {}
    return {
        "token": mask_token(token),
        "username": meta.get("username", "Not Tested"),
        "bot_id": meta.get("bot_id", ""),
        "status": meta.get("status", "Not Tested"),
        "last_error": meta.get("last_error", ""),
        "count": 1 if token else 0,
    }


@dataclass
class UploadRoute:
    kind: str
    label: str
    client: object
    chat_id: object = None
    thread_id: int | None = None
    token_key: str | None = None
    reserved: bool = False
    direct_final: bool = False
    notice: str = ""

    @property
    def direct(self):
        return self.kind in {"user_bot", "global_bot", "approved_helper"}


class StarFallXUploadManager:
    def __init__(self):
        self._lock = Lock()
        self._clients = {}
        self._active = {}
        self._cooldown = {}
        self._meta = {}
        self._global_hashes = set()
        self._main_active = 0
        self._pin_unlocked = {}
        self._last_queue_reason = ""

    def enabled(self):
        return str(Config.UPLOAD_ENGINE or "").lower() == "starfallx"

    def _cooling(self, key):
        return self._cooldown.get(key, 0) > time()

    def _token_key(self, scope, owner, token):
        return f"{scope}:{owner}:{token_hash(token)[:16]}"

    def _pin_required(self):
        return _safe_bool(getattr(Config, "HELPER_TOKEN_PIN_REQUIRED", True), True)

    def pin_is_set(self, user_id):
        return bool(user_data.get(user_id, {}).get(HELPER_PIN_HASH_KEY))

    def pin_unlocked(self, user_id):
        if not self._pin_required():
            return True
        return self._pin_unlocked.get(user_id, 0) > time()

    async def set_pin(self, user_id, pin):
        pin = str(pin or "").strip()
        if len(pin) < 4:
            raise ValueError("PIN must be at least 4 characters.")
        update_user_ldata(user_id, HELPER_PIN_HASH_KEY, _pin_hash(user_id, pin))
        self._pin_unlocked[user_id] = time() + 900
        await database.update_user_data(user_id)

    def verify_pin(self, user_id, pin):
        stored = user_data.get(user_id, {}).get(HELPER_PIN_HASH_KEY)
        if not stored:
            return False
        ok = stored == _pin_hash(user_id, str(pin or "").strip())
        if ok:
            self._pin_unlocked[user_id] = time() + 900
        return ok

    def lock_pin(self, user_id):
        self._pin_unlocked.pop(user_id, None)

    def _helper_limit(self):
        backups = max(0, _safe_int(getattr(Config, "HELPER_TOKEN_BACKUP_LIMIT", 5), 5))
        return 1 + backups

    def get_user_token_records(self, user_id, user_dict=None):
        user_dict = user_dict if user_dict is not None else user_data.get(user_id, {})
        changed = False
        records = user_dict.get(HELPER_TOKENS_KEY, []) or []
        if not isinstance(records, list):
            records = []
            changed = True

        normalized = []
        seen = set()
        for record in records:
            if not isinstance(record, dict) or not record.get("token"):
                changed = True
                continue
            fixed = _normalize_record(record)
            if fixed["token_hash"] in seen:
                changed = True
                continue
            seen.add(fixed["token_hash"])
            normalized.append(fixed)

        legacy_token = user_dict.get(USER_BOT_TOKEN_KEY)
        if legacy_token and token_hash(legacy_token) not in seen:
            meta = user_dict.get(USER_BOT_META_KEY, {}) or {}
            normalized.insert(
                0,
                _normalize_record(
                    {
                        "token": legacy_token,
                        "bot_id": meta.get("bot_id", ""),
                        "username": meta.get("username", "Not Tested"),
                        "status": meta.get("status", "Not Tested"),
                        "updated_at": meta.get("updated_at", int(time())),
                    },
                    primary=True,
                ),
            )
            changed = True

        primary_seen = False
        for index, record in enumerate(normalized):
            if record.get("primary") and not primary_seen:
                primary_seen = True
            elif record.get("primary"):
                record["primary"] = False
                changed = True
            elif index == 0 and not primary_seen:
                record["primary"] = True
                primary_seen = True
                changed = True

        if len(normalized) > self._helper_limit():
            normalized = normalized[: self._helper_limit()]
            changed = True

        if changed:
            user_dict[HELPER_TOKENS_KEY] = normalized
        return normalized

    def _ordered_records(self, records):
        return sorted(records, key=lambda item: (not item.get("primary"), item.get("updated_at", 0)))

    def _runtime_status(self, scope, owner, record):
        key = self._token_key(scope, owner, record.get("token"))
        if is_blacklisted(
            token=record.get("token"),
            bot_id=record.get("bot_id"),
            username=record.get("username"),
        ):
            return "Failed"
        if self._cooling(key):
            return "Cooling"
        if self._active.get(key, 0) > 0:
            return "Busy"
        return record.get("status", "Not Tested") or "Not Tested"

    def _public_records(self, user_id, user_dict=None):
        records = self.get_user_token_records(user_id, user_dict)
        safe = []
        for record in records:
            runtime = self._runtime_status("user", user_id, record)
            safe.append(_safe_record(record, runtime))
        return safe

    async def _save_records(self, user_id, records):
        update_user_ldata(user_id, HELPER_TOKENS_KEY, records)
        await database.update_user_data(user_id)

    async def _start_client(self, token, key, label):
        if key in self._clients:
            return self._clients[key]
        if is_blacklisted(token=token):
            raise ValueError("This upload bot token is blacklisted.")
        session_name = key.replace(":", "-")
        client = TgClient.wztgClient(
            f"StarFallX-{session_name}",
            bot_token=token,
            no_updates=True,
        )
        try:
            client = await TgClient._start_with_floodwait(client, label)
        except Exception:
            with contextlib.suppress(Exception):
                await client.stop()
            raise
        bot_id = getattr(client.me, "id", None)
        username = getattr(client.me, "username", "")
        if is_blacklisted(token=token, bot_id=bot_id, username=username):
            await client.stop()
            raise ValueError("This upload bot token is blacklisted.")
        self._clients[key] = client
        self._active.setdefault(key, 0)
        self._meta[key] = {"bot_id": bot_id, "username": username, "status": "Ready"}
        return client

    def _final_destination(self, listener):
        dest = getattr(listener, "leech_dest", None) or getattr(listener, "up_dest", None)
        thread_id = getattr(listener, "chat_thread_id", None)
        if not dest or dest == Config.LEECH_DUMP_CHAT:
            dest = getattr(getattr(listener, "message", None), "chat", None)
            dest = getattr(dest, "id", None) or getattr(listener, "user_id", None)
            thread_id = getattr(getattr(listener, "message", None), "message_thread_id", None)
        if not isinstance(dest, int):
            if "|" in str(dest):
                dest, thread = str(dest).split("|", 1)
                thread_id = int(thread) if thread.lstrip("-").isdigit() else None
            if str(dest).lstrip("-").isdigit():
                dest = int(dest)
            elif str(dest).lower() == "pm":
                dest = getattr(listener, "user_id", None)
        return dest, thread_id

    async def test_token(self, token, user_id=None, global_token=False, allow_direct=False):
        owner = "global" if global_token else user_id
        key = self._token_key("global" if global_token else "user", owner, token)
        client = await self._start_client(token, key, "StarFallX test bot")
        chat_id, thread_id = parse_dump_chat()
        if chat_id is None:
            if not allow_direct:
                raise ValueError("LEECH_DUMP_CHAT is not configured.")
            status = "Direct Only"
        else:
            try:
                msg = await client.send_message(
                    chat_id=chat_id,
                    text="StarFallX permission test.",
                    message_thread_id=thread_id,
                    disable_notification=True,
                )
                with contextlib.suppress(Exception):
                    await msg.delete()
                status = "Ready"
            except Exception as e:
                if not allow_direct:
                    raise
                status = "Direct Only" if _error_status(e) == "Not In Dump" else "Failed"
                if status == "Failed":
                    raise
        meta = self._meta.get(key, {})
        return {
            "key": key,
            "bot_id": meta.get("bot_id"),
            "username": meta.get("username"),
            "status": status,
        }

    async def set_user_token(self, user_id, token, primary=False):
        token = str(token or "").strip()
        if not token or ":" not in token:
            raise ValueError("Invalid bot token format.")
        info = await self.test_token(token, user_id=user_id, allow_direct=True)
        records = self.get_user_token_records(user_id)
        thash = token_hash(token)
        existing = next((record for record in records if record.get("token_hash") == thash), None)
        if existing:
            existing.update(
                _normalize_record(
                    {
                        "token": token,
                        "bot_id": info.get("bot_id"),
                        "username": info.get("username"),
                        "status": info.get("status", "Ready"),
                        "primary": existing.get("primary"),
                        "updated_at": int(time()),
                    }
                )
            )
        else:
            if len(records) >= self._helper_limit():
                raise ValueError(
                    f"Helper token limit reached: 1 primary + {Config.HELPER_TOKEN_BACKUP_LIMIT} backups."
                )
            records.append(
                _normalize_record(
                    {
                        "token": token,
                        "bot_id": info.get("bot_id"),
                        "username": info.get("username"),
                        "status": info.get("status", "Ready"),
                        "primary": primary or not records,
                        "updated_at": int(time()),
                    }
                )
            )
        if primary or not any(record.get("primary") for record in records):
            for record in records:
                record["primary"] = record.get("token_hash") == thash
        await self._save_records(user_id, records)
        return info

    async def remove_user_token(self, user_id, ident=None):
        records = self.get_user_token_records(user_id)
        if ident is None:
            targets = records[:1]
        else:
            ident = str(ident or "").strip().lower().lstrip("@")
            targets = []
            for index, record in enumerate(records, start=1):
                checks = {
                    str(index),
                    str(record.get("token_hash", "")).lower(),
                    str(record.get("mask", "")).lower(),
                    str(record.get("token", "")).split(":", 1)[0].lower(),
                    str(record.get("bot_id", "")).lower(),
                    str(record.get("username", "")).lower().lstrip("@"),
                }
                if ident in checks:
                    targets.append(record)
        target_hashes = {record.get("token_hash") for record in targets}
        for record in targets:
            key = self._token_key("user", user_id, record.get("token"))
            client = self._clients.pop(key, None)
            if client:
                with contextlib.suppress(Exception):
                    await client.stop()
            self._active.pop(key, None)
            self._cooldown.pop(key, None)
            self._meta.pop(key, None)
        records = [record for record in records if record.get("token_hash") not in target_hashes]
        if records and not any(record.get("primary") for record in records):
            records[0]["primary"] = True
        await self._save_records(user_id, records)
        user_dict = user_data.get(user_id, {})
        user_dict.pop(USER_BOT_TOKEN_KEY, None)
        user_dict.pop(USER_BOT_META_KEY, None)
        await database.update_user_data(user_id)
        return len(targets)

    async def set_primary_token(self, user_id, ident):
        ident = str(ident or "").strip().lower().lstrip("@")
        records = self.get_user_token_records(user_id)
        selected = None
        for index, record in enumerate(records, start=1):
            checks = {
                str(index),
                str(record.get("token_hash", "")).lower(),
                str(record.get("mask", "")).lower(),
                str(record.get("token", "")).split(":", 1)[0].lower(),
                str(record.get("bot_id", "")).lower(),
                str(record.get("username", "")).lower().lstrip("@"),
            }
            if ident in checks:
                selected = record
                break
        if not selected:
            raise ValueError("Helper token not found.")
        for record in records:
            record["primary"] = record is selected
        await self._save_records(user_id, records)
        return selected

    async def test_user_tokens(self, user_id):
        records = self.get_user_token_records(user_id)
        results = []
        for record in records:
            try:
                info = await self.test_token(record.get("token"), user_id=user_id, allow_direct=True)
                record.update(
                    {
                        "bot_id": info.get("bot_id"),
                        "username": info.get("username"),
                        "status": info.get("status", "Ready"),
                        "last_error": "",
                        "updated_at": int(time()),
                    }
                )
                results.append((record, True, info.get("status", "Ready")))
            except Exception as e:
                status = _error_status(e)
                record.update(
                    {
                        "status": status,
                        "last_error": str(e),
                        "updated_at": int(time()),
                    }
                )
                results.append((record, False, status))
        await self._save_records(user_id, records)
        return results

    async def ensure_global_clients(self):
        if not Config.GLOBAL_UPLOAD_BOT_ENABLED:
            return []
        tokens = [
            token.strip()
            for token in str(Config.GLOBAL_UPLOAD_BOT_TOKENS or "").split()
            if token.strip()
        ]
        clients = []
        current_hashes = {token_hash(token) for token in tokens}
        self._global_hashes = current_hashes
        for index, token in enumerate(tokens, start=1):
            if is_blacklisted(token=token):
                continue
            key = self._token_key("global", index, token)
            try:
                client = await self._start_client(token, key, f"StarFallX global bot {index}")
                clients.append((key, client))
            except Exception as e:
                LOGGER.error(f"Failed to start StarFallX global bot {index}: {e}")
        return clients

    async def add_global_token(self, token):
        info = await self.test_token(token, global_token=True)
        tokens = [
            item.strip()
            for item in str(Config.GLOBAL_UPLOAD_BOT_TOKENS or "").split()
            if item.strip()
        ]
        if token_hash(token) not in {token_hash(item) for item in tokens}:
            tokens.append(token)
        Config.GLOBAL_UPLOAD_BOT_TOKENS = " ".join(tokens)
        await database.update_config({"GLOBAL_UPLOAD_BOT_TOKENS": Config.GLOBAL_UPLOAD_BOT_TOKENS})
        return info

    async def remove_global_token(self, ident):
        tokens = [
            item.strip()
            for item in str(Config.GLOBAL_UPLOAD_BOT_TOKENS or "").split()
            if item.strip()
        ]
        ident = str(ident or "").strip().lower().lstrip("@")
        kept = []
        removed = []
        for index, token in enumerate(tokens, start=1):
            key = self._token_key("global", index, token)
            meta = self._meta.get(key, {})
            checks = {
                str(index),
                token_hash(token).lower(),
                mask_token(token).lower(),
                str(token).split(":", 1)[0].lower(),
                str(meta.get("bot_id", "")).lower(),
                str(meta.get("username", "")).lower().lstrip("@"),
            }
            if ident in checks:
                removed.append((key, token))
            else:
                kept.append(token)
        for key, _ in removed:
            client = self._clients.pop(key, None)
            if client:
                with contextlib.suppress(Exception):
                    await client.stop()
        Config.GLOBAL_UPLOAD_BOT_TOKENS = " ".join(kept)
        await database.update_config({"GLOBAL_UPLOAD_BOT_TOKENS": Config.GLOBAL_UPLOAD_BOT_TOKENS})
        return len(removed)

    async def add_blacklist(self, ident):
        items = [
            item.strip()
            for item in str(Config.UPLOAD_BOT_TOKEN_BLACKLIST or "").split()
            if item.strip()
        ]
        if ident not in items:
            items.append(ident)
        Config.UPLOAD_BOT_TOKEN_BLACKLIST = " ".join(items)
        await database.update_config({"UPLOAD_BOT_TOKEN_BLACKLIST": Config.UPLOAD_BOT_TOKEN_BLACKLIST})

    async def remove_blacklist(self, ident):
        ident_l = str(ident).strip().lower().lstrip("@")
        items = [
            item.strip()
            for item in str(Config.UPLOAD_BOT_TOKEN_BLACKLIST or "").split()
            if item.strip()
        ]
        kept = [
            item
            for item in items
            if item.lower().lstrip("@") != ident_l and token_hash(item).lower() != ident_l
        ]
        Config.UPLOAD_BOT_TOKEN_BLACKLIST = " ".join(kept)
        await database.update_config({"UPLOAD_BOT_TOKEN_BLACKLIST": Config.UPLOAD_BOT_TOKEN_BLACKLIST})
        return len(items) - len(kept)

    def _queue_guard_reason(self):
        total_limit = _safe_int(getattr(Config, "UPLOAD_MAX_ACTIVE_TOTAL", 0), 0)
        if total_limit > 0 and sum(self._active.values()) + self._main_active >= total_limit:
            return "Queue: upload limit"
        if _safe_bool(getattr(Config, "UPLOAD_SAFE_CPU_GUARD", True), True):
            overloaded, reason = resources_overloaded()
            if overloaded:
                return f"Queue: {reason}"
        return ""

    async def acquire_route(self, listener, file_size):
        if not self.enabled() or not getattr(listener, "is_leech", False):
            return UploadRoute("current", "Current Engine", listener.client)
        if file_size > BOT_UPLOAD_LIMIT:
            return UploadRoute(
                "current",
                "Premium User" if listener.user_transmission else "Current Engine",
                listener.client,
            )
        while True:
            async with self._lock:
                reason = self._queue_guard_reason()
                if reason:
                    self._last_queue_reason = reason
                else:
                    route = await self._try_acquire_locked(listener)
                    if route:
                        listener.upload_engine = f"{Config.UPLOAD_ENGINE} v{Config.UPLOAD_ENGINE_VERSION}"
                        listener.upload_client = route.label
                        return route
                    self._last_queue_reason = "Queue"
            if not Config.UPLOAD_QUEUE_ENABLED:
                return UploadRoute("current", "Current Engine", listener.client)
            listener.upload_engine = f"{Config.UPLOAD_ENGINE} v{Config.UPLOAD_ENGINE_VERSION}"
            listener.upload_client = self._last_queue_reason or "Queue"
            await sleep(2)

    async def _route_from_record(self, record, scope, owner, label_prefix, chat_id, thread_id, listener=None):
        token = record.get("token")
        if not token or is_blacklisted(
            token=token,
            bot_id=record.get("bot_id"),
            username=record.get("username"),
        ):
            return None
        key_scope = "user" if scope == "approved" else scope
        key = self._token_key(key_scope, owner, token)
        if self._cooling(key):
            return None
        if self._active.get(key, 0) >= 1:
            return None
        try:
            client = await self._start_client(token, key, f"StarFallX helper {owner}")
        except Exception as e:
            LOGGER.error(f"StarFallX helper bot unavailable: {e}")
            record["status"] = _error_status(e)
            record["last_error"] = str(e)
            return None
        meta = self._meta.get(key, {})
        username = meta.get("username") or record.get("username") or "HelperBot"
        status = record.get("status") or "Ready"
        direct_final = status == "Direct Only" or chat_id is None
        route_chat_id, route_thread_id = chat_id, thread_id
        notice = ""
        if direct_final and listener is not None:
            route_chat_id, route_thread_id = self._final_destination(listener)
            status = "Direct Only"
            notice = (
                f"StarFallX: @{username} is not in LEECH_DUMP_CHAT, so this upload "
                "will go directly to the final destination. Add the helper bot to "
                "the main dump to enable sequential dump/copy support."
            )
        record.update(
            {
                "bot_id": meta.get("bot_id") or record.get("bot_id"),
                "username": username,
                "status": status,
                "last_error": "",
                "updated_at": int(time()),
            }
        )
        self._active[key] = self._active.get(key, 0) + 1
        kind = "approved_helper" if scope == "approved" else "user_bot"
        return UploadRoute(
            kind,
            f"{label_prefix} @{username}",
            client,
            route_chat_id,
            route_thread_id,
            key,
            True,
            direct_final,
            notice,
        )

    async def _try_user_records(self, records, scope, owner, label_prefix, chat_id, thread_id, listener=None):
        for record in self._ordered_records(records):
            route = await self._route_from_record(record, scope, owner, label_prefix, chat_id, thread_id, listener)
            if route:
                return route
        return None

    async def _try_global_records(self, chat_id, thread_id):
        if not Config.GLOBAL_UPLOAD_BOT_ENABLED or chat_id is None:
            return None
        globals_ = await self.ensure_global_clients()
        candidates = []
        max_active = max(1, _safe_int(Config.GLOBAL_UPLOAD_BOT_MAX_ACTIVE, 1))
        for key, client in globals_:
            if self._cooling(key):
                continue
            active = self._active.get(key, 0)
            if active < max_active:
                candidates.append((active, key, client))
        if not candidates:
            return None
        _, key, client = sorted(candidates, key=lambda item: item[0])[0]
        self._active[key] = self._active.get(key, 0) + 1
        username = self._meta.get(key, {}).get("username", "GlobalBot")
        return UploadRoute(
            "global_bot",
            f"Global Bot @{username}",
            client,
            chat_id,
            thread_id,
            key,
            True,
        )

    async def _try_acquire_locked(self, listener):
        chat_id, thread_id = parse_dump_chat()
        user_id = listener.user_id
        is_owner_task = is_sudo_user(user_id)
        user_records = self.get_user_token_records(user_id, listener.user_dict)

        if Config.USER_BOT_TOKEN_UPLOAD:
            route = await self._try_user_records(
                user_records,
                "user",
                user_id,
                "Helper Bot",
                chat_id,
                thread_id,
                listener,
            )
            if route:
                return route

        if (
            is_owner_task
            and _safe_bool(getattr(Config, "HELPER_TOKEN_OWNER_CAN_USE_APPROVED", True), True)
        ):
            for owner_id, owner_dict in list(user_data.items()):
                if owner_id == user_id:
                    continue
                records = self.get_user_token_records(owner_id, owner_dict)
                route = await self._try_user_records(
                    records,
                    "approved",
                    owner_id,
                    "Approved Helper",
                    chat_id,
                    thread_id,
                    listener,
                )
                if route:
                    return route

        if (
            is_owner_task
            or _safe_bool(getattr(Config, "HELPER_TOKEN_NORMAL_USERS_GLOBAL_FALLBACK", False), False)
        ):
            route = await self._try_global_records(chat_id, thread_id)
            if route:
                return route

        max_main = max(0, _safe_int(Config.MAIN_BOT_FALLBACK_UPLOADS, 0))
        if max_main and self._main_active < max_main:
            self._main_active += 1
            return UploadRoute("current", "Main Bot", listener.client, reserved=True)
        return None

    async def release_route(self, route, failed=False, flood_wait=0):
        if not route or not route.reserved:
            return
        async with self._lock:
            if route.token_key:
                self._active[route.token_key] = max(0, self._active.get(route.token_key, 0) - 1)
                if failed:
                    cooldown = max(
                        int(Config.UPLOAD_BOT_COOLDOWN_SECONDS or 300),
                        int(flood_wait or 0),
                    )
                    self._cooldown[route.token_key] = time() + cooldown
                    if route.token_key in self._meta:
                        self._meta[route.token_key]["status"] = "Cooling"
                elif route.token_key in self._meta:
                    self._meta[route.token_key]["status"] = "Ready"
            else:
                self._main_active = max(0, self._main_active - 1)

    def status_summary(self):
        cooling = sum(1 for until in self._cooldown.values() if until > time())
        active_helpers = sum(self._active.values())
        ready = sum(1 for key in self._clients if not self._cooling(key) and self._active.get(key, 0) == 0)
        return {
            "engine": f"{Config.UPLOAD_ENGINE} v{Config.UPLOAD_ENGINE_VERSION}",
            "helper_active": active_helpers,
            "main_active": self._main_active,
            "cooling": cooling,
            "ready": ready,
            "clients": len(self._clients),
            "queue_reason": self._last_queue_reason,
        }

    async def stop_all(self):
        clients = list(self._clients.values())
        self._clients.clear()
        self._active.clear()
        self._cooldown.clear()
        self._meta.clear()
        self._main_active = 0
        for client in clients:
            with contextlib.suppress(Exception):
                await client.stop()

    def format_user_status(self, user_id):
        user_dict = user_data.get(user_id, {})
        records = self._public_records(user_id, user_dict)
        pin_state = "Unlocked" if self.pin_unlocked(user_id) else "Locked"
        lines = [
            f"<b>Engine:</b> {Config.UPLOAD_ENGINE} v{Config.UPLOAD_ENGINE_VERSION}",
            f"<b>PIN:</b> {pin_state if self._pin_required() else 'Not Required'}",
            f"<b>Rule:</b> primary first, then backups. One helper token = one active upload.",
            "",
            "<b>Token List</b>",
        ]
        if not records:
            lines.append("No helper tokens saved.")
        for index, record in enumerate(records, start=1):
            username = record.get("username") or "Not Tested"
            primary = "Primary" if record.get("primary") else f"Backup {index - 1}"
            lines.append(
                f"{index}. @{username} - <code>{record.get('token')}</code> | {primary} | {record.get('status')}"
            )
        lines.append("")
        lines.append("Add helper bots to LEECH_DUMP_CHAT for sequential support. Tokens not in dump show Direct Only.")
        return "\n".join(lines)


starfallx_upload = StarFallXUploadManager()
