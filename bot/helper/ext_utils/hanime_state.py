from asyncio import Lock
from contextlib import asynccontextmanager
from time import time


_hanime_bulk_lock = Lock()
_hanime_upload_lock = Lock()
_hanime_bulk = {"active": False, "owner_id": 0, "letter": "", "started": 0}


def hanime_bulk_active():
    return bool(_hanime_bulk["active"])


def hanime_bulk_context():
    return dict(_hanime_bulk)


@asynccontextmanager
async def hanime_bulk_run(owner_id, letter):
    async with _hanime_bulk_lock:
        if _hanime_bulk["active"]:
            raise RuntimeError("Hanime letter leech is already running.")
        _hanime_bulk.update(
            {
                "active": True,
                "owner_id": int(owner_id or 0),
                "letter": str(letter or "").upper(),
                "started": time(),
            }
        )
    try:
        yield
    finally:
        async with _hanime_bulk_lock:
            _hanime_bulk.update(
                {"active": False, "owner_id": 0, "letter": "", "started": 0}
            )


@asynccontextmanager
async def hanime_upload_slot(enabled=True):
    if not enabled:
        yield
        return
    async with _hanime_upload_lock:
        yield
