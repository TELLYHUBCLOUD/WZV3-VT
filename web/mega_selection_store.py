# Adapted from irisXDR/NEO-WZML (AGPL-3.0).

import json
import os
import time


_BASE_DIR = "/usr/src/app/downloads/.mega_selections"
_STALE_AFTER_SECONDS = 6 * 60 * 60


def _safe_gid(gid):
    return bool(gid) and all(char.isalnum() or char in "-_" for char in gid)


def _path(gid):
    return os.path.join(_BASE_DIR, f"{gid}.json")


def write_state(gid, file_list, selected_ids=()):
    if not _safe_gid(gid):
        return False
    tmp = ""
    try:
        os.makedirs(_BASE_DIR, exist_ok=True)
        target = _path(gid)
        tmp = f"{target}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(
                {"file_list": file_list, "selected_ids": list(selected_ids)},
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
        return True
    except OSError:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def read_state(gid):
    if not _safe_gid(gid):
        return None
    try:
        with open(_path(gid), encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def update_selected_ids(gid, selected_ids):
    data = read_state(gid)
    if data is None:
        return False
    return write_state(gid, data.get("file_list", []), selected_ids)


def delete_state(gid):
    if _safe_gid(gid):
        try:
            os.remove(_path(gid))
        except OSError:
            pass


def cleanup_stale_states(max_age=_STALE_AFTER_SECONDS):
    if not os.path.isdir(_BASE_DIR):
        return 0
    cutoff = time.time() - max_age
    removed = 0
    for entry in os.scandir(_BASE_DIR):
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
                removed += 1
        except OSError:
            continue
    return removed
