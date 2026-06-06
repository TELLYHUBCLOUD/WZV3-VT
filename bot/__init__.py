# ruff: noqa: E402

from uvloop import install

install()

from asyncio import new_event_loop, set_event_loop

bot_loop = new_event_loop()
set_event_loop(bot_loop)

from subprocess import run as srun
from os import getcwd
from asyncio import Lock
from logging import (
    ERROR,
    INFO,
    WARNING,
    FileHandler,
    StreamHandler,
    basicConfig,
    getLogger,
)
from os import cpu_count
from time import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .core.config_manager import BinConfig
from sabnzbdapi import SabnzbdClient

getLogger("requests").setLevel(WARNING)
getLogger("urllib3").setLevel(WARNING)
getLogger("pyrogram").setLevel(ERROR)
getLogger("aiohttp").setLevel(ERROR)
getLogger("apscheduler").setLevel(ERROR)
getLogger("httpx").setLevel(WARNING)
getLogger("pymongo").setLevel(WARNING)
getLogger("aiohttp").setLevel(WARNING)


bot_start_time = time()

bot_loop = new_event_loop()
set_event_loop(bot_loop)

basicConfig(
    format="[%(asctime)s] [%(levelname)s] - %(message)s",  #  [%(filename)s:%(lineno)d]
    datefmt="%d-%b-%y %I:%M:%S %p",
    handlers=[FileHandler("log.txt"), StreamHandler()],
    level=INFO,
)

LOGGER = getLogger(__name__)
cpu_no = cpu_count()
threads = max(1, cpu_no // 2)
cores = ",".join(str(i) for i in range(threads))

bot_cache = {}
DOWNLOAD_DIR = "/usr/src/app/downloads/"
intervals = {"status": {}, "qb": "", "jd": "", "nzb": "", "stopAll": False}
qb_torrents = {}
jd_downloads = {}
nzb_jobs = {}
user_data = {}
aria2_options = {}
qbit_options = {}
nzb_options = {}
queued_dl = {}
queued_up = {}
status_dict = {}
task_dict = {}
rss_dict = {}
shortener_dict = {}
var_list = [
    "BOT_TOKEN",
    "TELEGRAM_API",
    "TELEGRAM_HASH",
    "OWNER_ID",
    "DATABASE_URL",
    "BASE_URL",
    "UPSTREAM_REPO",
    "UPSTREAM_BRANCH",
    "UPDATE_PKGS",
    "AUTO_THUMBNAIL",
    "AUTO_THUMBNAIL_QUALITY",
    "TMDB_ACCESS_TOKEN",
    "AUTORENAME",
    "RENAME_METHOD",
    "LEECH_FILENAME_REMNAME_AUTO",
    "LEECH_FILENAME_REMNAME_REGEX",
    "SUBTITLE_TRANSLATE_TARGET",
    "INTRO_SUBTITLE_TEXT",
    "UPLOAD_ENGINE",
    "UPLOAD_ENGINE_VERSION",
    "USER_BOT_TOKEN_UPLOAD",
    "USER_BOT_TOKEN_MAX_ACTIVE",
    "HELPER_TOKEN_PIN_REQUIRED",
    "HELPER_TOKEN_BACKUP_LIMIT",
    "HELPER_TOKEN_OWNER_CAN_USE_APPROVED",
    "HELPER_TOKEN_NORMAL_USERS_GLOBAL_FALLBACK",
    "GLOBAL_UPLOAD_BOT_TOKENS",
    "GLOBAL_UPLOAD_BOT_ENABLED",
    "GLOBAL_UPLOAD_BOT_MAX_ACTIVE",
    "MAIN_BOT_FALLBACK_UPLOADS",
    "UPLOAD_QUEUE_ENABLED",
    "UPLOAD_MAX_ACTIVE_TOTAL",
    "UPLOAD_SAFE_CPU_GUARD",
    "UPLOAD_BOT_TOKEN_BLACKLIST",
    "UPLOAD_BOT_COOLDOWN_SECONDS",
    "UPLOAD_PRIVATE_DUMP_ONLY_KEYWORDS",
    "UPLOAD_PRIVATE_DUMP_ONLY_DOMAINS",
    "OWNER_SESSION_STRINGS",
    "OWNER_HELPER_BOT_TOKENS",
    "PERFORMANCE_PROFILE",
    "FFMPEG_THREADS",
    "FFMPEG_CPU_CORES",
    "TG_COPY_DELAY",
    "TG_FLOOD_WAIT_MULTIPLIER",
    "MAX_PARALLEL_TASKS",
    "SAFE_CPU_PERCENT",
    "SAFE_FREE_RAM_MB",
    "ARIA2_MAX_CONNECTION_PER_SERVER",
    "ARIA2_SPLIT",
    "ARIA2_MIN_SPLIT_SIZE",
    "ARIA2_MAX_CONCURRENT_DOWNLOADS",
    "ARIA2_MAX_OVERALL_DOWNLOAD_LIMIT",
    "ARIA2_MAX_OVERALL_UPLOAD_LIMIT",
    "QBIT_UPLOAD_LIMIT",
    "STATUS_THEME",
    "CTORRENT_STORAGE_DIR",
    "CTORRENT_OUTPUT_DIR",
    "CTORRENT_TRACKERS",
    "CTORRENT_PRIVATE",
    "CTORRENT_KEEP_SOURCE",
    "CTORRENT_AUTO_ADD_QBIT",
    "CTORRENT_BBCODE_TEMPLATE",
    "CTORRENT_BBCODE_TEMPLATE_PATH",
    "LIBRE_TRANSLATE_API_URL",
    "LIBRE_TRANSLATE_API_KEY",
    "MYANIMELIST_CLIENT_ID",
    "MYANIMELIST_CLIENT_NAME",
    "FFMPEG_QUEUE_ENABLED",
    "FFMPEG_QUEUE_LOGS",
    "VT_MERGE_TRACK_TIMEOUT",
    "VIDEO_TOOLS_REPLY_TIMEOUT",
    "VIDEO_TOOLS_LOGS",
    "AUTO_PROCESS_MESSAGE_MODE",
    "AUTO_PROCESS_LOGS",
    "AUTO_VT",
    "AUTO_ORDER",
    "AUTO_AUDIO_ORDER",
    "AUTO_SUBTITLE_ORDER",
    "BATCH_TASK_RESTART_RESUME",
    "BLEECH_MAX_ACTIVE_DOWNLOADS",
    "BLEECH_MAX_ACTIVE_UPLOADS",
    "BLEECH_LINK_SIZE_LIMIT_GB",
    "BQLEECH_BATCH_SIZE_GB",
    "BQLEECH_MAX_ACTIVE_DOWNLOADS",
    "BQLEECH_MAX_ACTIVE_UPLOADS",
    "SUBTITLE_TRANSLATE_PROVIDER",
    "AUTO_PROCESS",
    "AUTO_LEECH",
    "AUTO_UNZIP",
    "AUTO_REMOVE_STREAMS",
    "AUTO_KEEP_AUDIO_LANGS",
    "AUTO_KEEP_SUBTITLE_LANGS",
    "AUTO_MERGE",
    "AUTO_MERGE_FILENAME",
    "AUTO_MERGE_SAFETY_MB",
    "AUTO_INTRO_SUBTITLE",
    "AUTO_METADATA",
    "AUTO_RENAME",
    "INTRO_SUBTITLE_DURATION",
    "INTRO_SUBTITLE_RANGES",
    "INTRO_SUBTITLE_FADE_MS",
    "INTRO_SUBTITLE_FONT",
    "INTRO_SUBTITLE_FONT_SIZE",
    "INTRO_SUBTITLE_COLOR",
    "INTRO_SUBTITLE_OUTLINE_COLOR",
    "INTRO_SUBTITLE_COLOR_PALETTE",
]
auth_chats = {}
excluded_extensions = ["aria2", "!qB"]
drives_names = []
drives_ids = []
index_urls = []
sudo_users = []
non_queued_dl = set()
non_queued_up = set()
multi_tags = set()
task_dict_lock = Lock()
queue_dict_lock = Lock()
qb_listener_lock = Lock()
nzb_listener_lock = Lock()
jd_listener_lock = Lock()
cpu_eater_lock = Lock()
same_directory_lock = Lock()

sabnzbd_client = SabnzbdClient(
    host="http://localhost",
    api_key="admin",
    port="8070",
)
srun([BinConfig.QBIT_NAME, "-d", f"--profile={getcwd()}"], check=False)

scheduler = AsyncIOScheduler(event_loop=bot_loop)
