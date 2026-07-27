from asyncio import Lock, sleep


class HstreamMaintenance:
    def __init__(self):
        self._lock = Lock()
        self.requested = False
        self.active = False
        self.owner_id = None

    async def request(self, owner_id):
        async with self._lock:
            if self.requested:
                return False
            self.requested = True
            self.active = False
            self.owner_id = owner_id
            return True

    async def wait_until_idle(self, cancel_event, progress=None):
        from ... import (
            non_queued_dl,
            non_queued_up,
            queued_dl,
            queued_up,
            rss_non_queued_dl,
            rss_non_queued_up,
            rss_queued_dl,
            rss_queued_up,
        )

        while not cancel_event.is_set():
            counts = (
                len(non_queued_dl),
                len(non_queued_up),
                len(queued_dl),
                len(queued_up),
                len(rss_non_queued_dl),
                len(rss_non_queued_up),
                len(rss_queued_dl),
                len(rss_queued_up),
            )
            if not any(counts):
                async with self._lock:
                    self.active = True
                return True
            if progress:
                await progress(sum(counts[:4]), sum(counts[4:]))
            await sleep(2)
        return False

    async def release(self):
        async with self._lock:
            self.requested = False
            self.active = False
            self.owner_id = None

    def blocks_tasks(self):
        return self.requested

    def pauses_feeds(self):
        return self.requested


hstream_maintenance = HstreamMaintenance()
