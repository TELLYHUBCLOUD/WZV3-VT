# REQUIRED CONFIG
BOT_TOKEN = ""
OWNER_ID = 0
TELEGRAM_API = 0
TELEGRAM_HASH = ""
DATABASE_URL = ""

# OPTIONAL CONFIG
DEFAULT_LANG = "en"
TG_PROXY = (
    {}
)  # {"scheme": ”socks5”, "hostname": ””, "port": 1234, "username": ”user”, "password": ”pass”}
USER_SESSION_STRING = ""
CMD_SUFFIX = ""
AUTHORIZED_CHATS = ""
SUDO_USERS = ""
STATUS_LIMIT = 10
DEFAULT_UPLOAD = "rc"
STATUS_UPDATE_INTERVAL = 15
FILELION_API = ""
STREAMWISH_API = ""
EXCLUDED_EXTENSIONS = ""
INCOMPLETE_TASK_NOTIFIER = False
YT_DLP_OPTIONS = ""
USE_SERVICE_ACCOUNTS = False
NAME_SWAP = ""
FFMPEG_CMDS = {
    "t": [
        "-threads 0 -i mltb.video -map 0:v:0 -map 0:a:m:language:tam -map 0:s:m:language:eng -c copy -max_muxing_queue_size 9999 mltb.mkv -del"
    ]
}
UPLOAD_PATHS = {}

# StarFallX Upload Engine
UPLOAD_ENGINE = "StarFallX"
UPLOAD_ENGINE_VERSION = "1.2"
USER_BOT_TOKEN_UPLOAD = True
USER_BOT_TOKEN_MAX_ACTIVE = 1
HELPER_TOKEN_PIN_REQUIRED = True
HELPER_TOKEN_BACKUP_LIMIT = 5
HELPER_TOKEN_OWNER_CAN_USE_APPROVED = True
HELPER_TOKEN_NORMAL_USERS_GLOBAL_FALLBACK = False
GLOBAL_UPLOAD_BOT_TOKENS = ""  # owner/sudo global upload bot tokens, space separated
GLOBAL_UPLOAD_BOT_ENABLED = True
GLOBAL_UPLOAD_BOT_MAX_ACTIVE = 1
MAIN_BOT_FALLBACK_UPLOADS = 1
UPLOAD_QUEUE_ENABLED = True
UPLOAD_MAX_ACTIVE_TOTAL = 0
UPLOAD_SAFE_CPU_GUARD = True
UPLOAD_BOT_TOKEN_BLACKLIST = ""  # token, bot id, or @username separated by spaces
UPLOAD_BOT_COOLDOWN_SECONDS = 300
UPLOAD_PRIVATE_DUMP_ONLY_KEYWORDS = ""
UPLOAD_PRIVATE_DUMP_ONLY_DOMAINS = ""

# Performance profile
# auto: choose safe/balanced/max_speed from VPS CPU/RAM.
# max_speed: faster Telegram leech with CPU/RAM safety guards.
# balanced: lower parallelism.
# safe: lowest CPU pressure.
PERFORMANCE_PROFILE = "auto"
FFMPEG_THREADS = 0  # 0 = auto from profile. Example: 2 or 3 for fixed low CPU.
FFMPEG_CPU_CORES = ""  # empty = auto. Example: "0,1" to pin FFmpeg.
TG_COPY_DELAY = 0.15  # delay between sequential dump -> user copies.
TG_FLOOD_WAIT_MULTIPLIER = 1.1
MAX_PARALLEL_TASKS = 4
SAFE_CPU_PERCENT = 92
SAFE_FREE_RAM_MB = 512

# Aria2 max-speed but safe defaults. Direct downloads use more connections,
# while torrent upload bandwidth is capped so Telegram uploads stay fast.
ARIA2_MAX_CONNECTION_PER_SERVER = 16
ARIA2_SPLIT = 16
ARIA2_MIN_SPLIT_SIZE = "1M"
ARIA2_MAX_CONCURRENT_DOWNLOADS = 4
ARIA2_MAX_OVERALL_DOWNLOAD_LIMIT = "0"
ARIA2_MAX_OVERALL_UPLOAD_LIMIT = "1M"
QBIT_UPLOAD_LIMIT = 1048576
STATUS_THEME = "starfall"  # classic, starfall, compact

# Create Torrent release pack
CTORRENT_STORAGE_DIR = "/usr/src/app/torrents/seeding"
CTORRENT_OUTPUT_DIR = "/usr/src/app/torrents/output"
CTORRENT_TRACKERS = """
udp://tracker.opentrackr.org:1337/announce
udp://open.stealth.si:80/announce
udp://tracker.torrent.eu.org:451/announce
udp://open.demonii.com:1337/announce
"""
CTORRENT_PRIVATE = False
CTORRENT_KEEP_SOURCE = True
CTORRENT_AUTO_ADD_QBIT = False
CTORRENT_BBCODE_TEMPLATE = "anime_release"  # classic, dark_bg, anime_release, minimal
CTORRENT_BBCODE_TEMPLATE_PATH = ""  # Optional custom .txt/.bbcode template path

# Subtitle translation
# LibreTranslate endpoint examples:
# https://libretranslate.com or https://your-domain.com
LIBRE_TRANSLATE_API_URL = ""
LIBRE_TRANSLATE_API_KEY = ""
SUBTITLE_TRANSLATE_PROVIDER = "libre"

# MyAnimeList official API fallback for anime thumbnails.
# Optional until your MAL app/client is approved.
MYANIMELIST_CLIENT_ID = ""
MYANIMELIST_CLIENT_NAME = ""

# Video Tools / Auto Process
VT_MERGE_TRACK_TIMEOUT = 60
VIDEO_TOOLS_REPLY_TIMEOUT = 30
VIDEO_TOOLS_LOGS = True
AUTO_PROCESS_MESSAGE_MODE = "quiet"
AUTO_PROCESS_LOGS = False
AUTO_VT = False
AUTO_ORDER = False
AUTO_AUDIO_ORDER = ""  # Example: tam tel eng
AUTO_SUBTITLE_ORDER = ""  # Example: eng tam

# Batch Leech, normal link queue
BATCH_TASK_RESTART_RESUME = True  # Resume /bleech and /bqleech plans after bot restart.
BLEECH_MAX_ACTIVE_DOWNLOADS = 1
BLEECH_MAX_ACTIVE_UPLOADS = 2
BLEECH_LINK_SIZE_LIMIT_GB = 0  # 0 = no per-link size limit

# Big Queue Leech, qBittorrent only
BQLEECH_BATCH_SIZE_GB = 30
BQLEECH_MAX_ACTIVE_DOWNLOADS = 1
BQLEECH_MAX_ACTIVE_UPLOADS = 1

# Auto Process
AUTO_PROCESS = False
AUTO_LEECH = False
AUTO_UNZIP = False
AUTO_REMOVE_STREAMS = False
AUTO_KEEP_AUDIO_LANGS = ""  # Example: tam,ta,tamil
AUTO_KEEP_SUBTITLE_LANGS = ""  # Example: eng,en,english
AUTO_MERGE = False
AUTO_MERGE_FILENAME = ""  # Empty = use detected title. Supports {title}, {season}, {start}, {end}
AUTO_MERGE_SAFETY_MB = 150
AUTO_INTRO_SUBTITLE = False
AUTO_METADATA = True
AUTO_RENAME = True
INTRO_SUBTITLE_DURATION = 5
INTRO_SUBTITLE_RANGES = "00:00:00 - 00:00:05 (5s) | 00:01:20 - 00:01:25 (5s) | 00:02:40 - 00:02:45 (5s) | 00:04:00 - 00:04:05 (5s) | 00:05:20 - 00:05:25 (5s) | 00:06:40 - 00:06:45 (5s) | 00:08:00 - 00:08:05 (5s) | 00:09:20 - 00:09:25 (5s) | 00:10:40 - 00:10:45 (5s) | 00:12:00 - 00:12:05 (5s) | 00:13:20 - 00:13:25 (5s) | 00:14:40 - 00:14:45 (5s) | 00:16:00 - 00:16:05 (5s) | 00:17:20 - 00:17:25 (5s) | 00:18:40 - 00:18:45 (5s)"
INTRO_SUBTITLE_FADE_MS = 400
INTRO_SUBTITLE_FONT = "Arial"
INTRO_SUBTITLE_FONT_SIZE = 36
INTRO_SUBTITLE_COLOR = "&H00FFFFFF"
INTRO_SUBTITLE_OUTLINE_COLOR = "&H00000000"
INTRO_SUBTITLE_COLOR_PALETTE = ""  # Example: "#ff4aa2,#4ad8ff,#fff176"

# FFmpeg safety
FFMPEG_QUEUE_ENABLED = True
FFMPEG_QUEUE_LOGS = True

# Owner/session structured config placeholders. Existing single-session config still works.
OWNER_SESSION_STRINGS = ""
OWNER_HELPER_BOT_TOKENS = ""

# Hyper Tg Downloader
HELPER_TOKENS = ""

# MegaAPI v4.30
MEGA_EMAIL = ""
MEGA_PASSWORD = ""

# Disable Options
DISABLE_TORRENTS = False
DISABLE_LEECH = False
DISABLE_BULK = False
DISABLE_MULTI = False
DISABLE_SEED = False
DISABLE_FF_MODE = False

# Telegraph
AUTHOR_NAME = "WZML-X"
AUTHOR_URL = "https://t.me/WZML_X"

# Task Limits
DIRECT_LIMIT = 0
MEGA_LIMIT = 0
TORRENT_LIMIT = 0
GD_DL_LIMIT = 0
RC_DL_LIMIT = 0
CLONE_LIMIT = 0
JD_LIMIT = 0
NZB_LIMIT = 0
YTDLP_LIMIT = 0
PLAYLIST_LIMIT = 0
LEECH_LIMIT = 0
EXTRACT_LIMIT = 0
ARCHIVE_LIMIT = 0
STORAGE_LIMIT = 0

# Insta video downloader api
INSTADL_API = ""

# Nzb search
HYDRA_IP = ""
HYDRA_API_KEY = ""

# Media Search
IMDB_TEMPLATE = """<b>Title: </b> {title} [{year}]
<b>Also Known As:</b> {aka}
<b>Rating ⭐️:</b> <i>{rating}</i>
<b>Release Info: </b> <a href="{url_releaseinfo}">{release_date}</a>
<b>Genre: </b>{genres}
<b>IMDb URL:</b> {url}
<b>Language: </b>{languages}
<b>Country of Origin : </b> {countries}

<b>Story Line: </b><code>{plot}</code>

<a href="{url_cast}">Read More ...</a>"""

# Task Tools
FORCE_SUB_IDS = ""
MEDIA_STORE = True
DELETE_LINKS = False
CLEAN_LOG_MSG = False

# Limiters
BOT_MAX_TASKS = 0
USER_MAX_TASKS = 0
USER_TIME_INTERVAL = 0
VERIFY_TIMEOUT = 0
LOGIN_PASS = ""

# Bot Settings
BOT_PM = False
SET_COMMANDS = True
TIMEZONE = "Asia/Kolkata"
LEECH_COMPLETE_MSG = True
SEQUENTIAL_LEECH = True

# GDrive Tools
GDRIVE_ID = ""
GD_DESP = "Uploaded with WZ Bot"
IS_TEAM_DRIVE = False
STOP_DUPLICATE = False
INDEX_URL = ""

# YT Tools
YT_DESP = "Uploaded to YouTube by WZML-X bot"
YT_TAGS = ["telegram", "bot", "youtube"]  # or as a comma-separated string
YT_CATEGORY_ID = 22
YT_PRIVACY_STATUS = "unlisted"

# Rclone
RCLONE_PATH = ""
RCLONE_FLAGS = ""
RCLONE_SERVE_URL = ""
SHOW_CLOUD_LINK = True
RCLONE_SERVE_PORT = 0
RCLONE_SERVE_USER = ""
RCLONE_SERVE_PASS = ""

# JDownloader
JD_EMAIL = ""
JD_PASS = ""

# Sabnzbd
USENET_SERVERS = [
    {
        "name": "main",
        "host": "",
        "port": 563,
        "timeout": 60,
        "username": "",
        "password": "",
        "connections": 8,
        "ssl": 1,
        "ssl_verify": 2,
        "ssl_ciphers": "",
        "enable": 1,
        "required": 0,
        "optional": 0,
        "retention": 0,
        "send_group": 0,
        "priority": 0,
    }
]

# Update
UPSTREAM_REPO = ""
UPSTREAM_BRANCH = "master"
UPDATE_PKGS = True

# Leech
LEECH_SPLIT_SIZE = 0
AS_DOCUMENT = False
EQUAL_SPLITS = False
MEDIA_GROUP = False
USER_TRANSMISSION = True
HYBRID_LEECH = True
LEECH_PREFIX = ""
LEECH_SUFFIX = ""
LEECH_FONT = ""
LEECH_CAPTION = ""
THUMBNAIL_LAYOUT = ""

# Auto Rename and Thumbnail
AUTO_THUMBNAIL = False
AUTO_THUMBNAIL_QUALITY = 95
TMDB_ACCESS_TOKEN = ""
AUTORENAME = True
RENAME_METHOD = "auto"
LEECH_FILENAME_REMNAME_AUTO = "[S{season}E{episode}] {title}   {resolution} {bit} {ott} {quality} {lib} [Tamil] ESub"
LEECH_FILENAME_REMNAME_REGEX = ""
SUBTITLE_TRANSLATE_TARGET = "en"
INTRO_SUBTITLE_TEXT = ""

# Log Channels
LEECH_DUMP_CHAT = ""
LINKS_LOG_ID = ""
MIRROR_LOG_ID = ""

# qBittorrent/Aria2c
TORRENT_TIMEOUT = 0
BASE_URL = ""
BASE_URL_PORT = 0
WEB_PINCODE = True

# Queueing system
QUEUE_ALL = 0
QUEUE_DOWNLOAD = 0
QUEUE_UPLOAD = 0

# RSS
RSS_DELAY = 600
RSS_CHAT = ""
RSS_SIZE_LIMIT = 0

# Torrent Search
SEARCH_API_LINK = ""
SEARCH_LIMIT = 0
SEARCH_PLUGINS = [
    "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/piratebay.py",
    "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/limetorrents.py",
    "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/torlock.py",
    "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/torrentscsv.py",
    "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/eztv.py",
    "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/torrentproject.py",
    "https://raw.githubusercontent.com/MaurizioRicci/qBittorrent_search_engines/master/kickass_torrent.py",
    "https://raw.githubusercontent.com/MaurizioRicci/qBittorrent_search_engines/master/yts_am.py",
    "https://raw.githubusercontent.com/MadeOfMagicAndWires/qBit-plugins/master/engines/linuxtracker.py",
    "https://raw.githubusercontent.com/MadeOfMagicAndWires/qBit-plugins/master/engines/nyaasi.py",
    "https://raw.githubusercontent.com/LightDestory/qBittorrent-Search-Plugins/master/src/engines/ettv.py",
    "https://raw.githubusercontent.com/LightDestory/qBittorrent-Search-Plugins/master/src/engines/glotorrents.py",
    "https://raw.githubusercontent.com/LightDestory/qBittorrent-Search-Plugins/master/src/engines/thepiratebay.py",
    "https://raw.githubusercontent.com/v1k45/1337x-qBittorrent-search-plugin/master/leetx.py",
    "https://raw.githubusercontent.com/nindogo/qbtSearchScripts/master/magnetdl.py",
    "https://raw.githubusercontent.com/msagca/qbittorrent_plugins/main/uniondht.py",
    "https://raw.githubusercontent.com/khensolomon/leyts/master/yts.py",
]
