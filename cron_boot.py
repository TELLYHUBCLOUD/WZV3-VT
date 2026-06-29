from os import getenv, getppid, kill
from signal import SIGTERM
from time import monotonic, sleep
from requests import get as rget
from logging import error as logerror

BASE_URL = getenv("BASE_URL", None)
try:
    if len(BASE_URL) == 0:
        raise TypeError
    BASE_URL = BASE_URL.rstrip("/")
except TypeError:
    BASE_URL = None

PORT = getenv("PORT", None)
if PORT is not None and BASE_URL is not None:
    interval = int(getenv("AUTO_RESTART_INTERVAL", "300") or "300")
    check_delay = min(max(interval, 60), 300)
    failed_since = None
    while True:
        try:
            status = rget(BASE_URL, timeout=20).status_code
            if status >= 500:
                raise RuntimeError(f"HTTP {status}")
            failed_since = None
            sleep(check_delay)
        except Exception as e:
            logerror(f"cron_boot.py: {e}")
            now = monotonic()
            if failed_since is None:
                failed_since = now
            if now - failed_since >= interval:
                logerror("cron_boot.py: app unhealthy for 5 minutes, restarting bot")
                kill(getppid(), SIGTERM)
                sleep(30)
            sleep(10)
