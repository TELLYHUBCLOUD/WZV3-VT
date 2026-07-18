import json
from asyncio import sleep
from contextlib import suppress
from functools import partial
from html import escape
from io import BytesIO
from os import getcwd
from re import search, sub
from time import time
from zipfile import ZIP_DEFLATED, ZipFile

from aiofiles.os import makedirs, remove
from aiofiles.os import path as aiopath
from langcodes import Language
from pyrogram.enums import ButtonStyle
from pyrogram.filters import create
from pyrogram.handlers import MessageHandler

from bot.helper.ext_utils.status_utils import get_readable_file_size

GREEN_DOT = "\U0001F7E2"
RED_DOT = "\U0001F534"
CHECK_MARK = "\u2705"
CROSS_MARK = "\u274C"


def state_icon(enabled):
    return GREEN_DOT if enabled else RED_DOT


def state_style(enabled):
    return ButtonStyle.SUCCESS if enabled else ButtonStyle.DANGER


def state_label(label, enabled):
    return f"{state_icon(enabled)} {label}"


from .. import auth_chats, excluded_extensions, sudo_users, user_data
from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import (
    get_size_bytes,
    new_task,
    update_user_ldata,
)
from ..helper.ext_utils.db_handler import database
from ..helper.ext_utils.media_utils import create_thumb
from ..helper.ext_utils.starfallx_upload import (
    HELPER_PIN_HASH_KEY,
    HELPER_TOKENS_KEY,
    USER_BOT_TOKEN_KEY,
    safe_helper_tokens,
    safe_user_token_data,
    starfallx_upload,
)
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_file,
    send_message,
)

handler_dict = {}


def config_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


TEMPLATE_VARIABLES_TEXT = (
    "{filename} {upload_filename} {file_name} {file_size} {file_caption} "
    "{languages} {subtitles} {duration} {ott} {resolution} {name} {title} "
    "{year} {quality} {DS4K} {season} {episode} {audio} {lib} {extension} "
    "{shortsub} {shortlang} {part} {raw_name} {link} {vcodec} {codec} "
    "{acodec} {audio_codec} {audio_channels} {audio_bitrate} {hdr} "
    "{dynamic_range} {release_group} {group} {start} {end} {range} "
    "{date} {episode_name}"
)

leech_options = [
    "THUMBNAIL",
    "LEECH_SPLIT_SIZE",
    "LEECH_DUMP_CHAT",
    "LEECH_PREFIX",
    "LEECH_SUFFIX",
    "LEECH_CAPTION",
    "LEECH_FONT",
    "THUMBNAIL_LAYOUT",
    "lremname_auto",
    "lremname_regex",
    "SUBTITLE_TRANSLATE_TARGET",
    "INTRO_SUBTITLE_TEXT",
]
post_options = [
    "POST_MOVIE_CAPTION",
    "POST_ANIME_CAPTION",
    "POST_TV_CAPTION",
    "POST_BRAND_NAME",
    "POST_LOGO",
]
auto_process_options = [
    "AUTO_KEEP_AUDIO_LANGS",
    "AUTO_KEEP_SUBTITLE_LANGS",
    "AUTO_AUDIO_ORDER",
    "AUTO_SUBTITLE_ORDER",
    "INTRO_SUBTITLE_RANGES",
]
uphoster_options = [
    "GOFILE_TOKEN",
    "GOFILE_FOLDER_ID",
    "BUZZHEAVIER_TOKEN",
    "BUZZHEAVIER_FOLDER_ID",
    "PIXELDRAIN_KEY",
]
rclone_options = ["RCLONE_CONFIG", "RCLONE_PATH", "RCLONE_FLAGS"]
gdrive_options = ["TOKEN_PICKLE", "GDRIVE_ID", "INDEX_URL"]
ffset_options = [
    "METADATA",
    "AUDIO_METADATA",
    "VIDEO_METADATA",
    "SUBTITLE_METADATA",
]
advanced_options = [
    "EXCLUDED_EXTENSIONS",
    "NAME_SWAP",
    "YT_DLP_OPTIONS",
    "UPLOAD_PATHS",
    "USER_COOKIE_FILE",
]
yt_options = ["YT_DESP", "YT_TAGS", "YT_CATEGORY_ID", "YT_PRIVACY_STATUS"]

user_settings_text = {
    "THUMBNAIL": (
        "Photo or Doc",
        "Custom Thumbnail is used as the thumbnail for the files you upload to telegram in media or document mode.",
        "<i>Send a photo to save it as custom thumbnail.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "RCLONE_CONFIG": (
        "",
        "",
        "<i>Send your <code>rclone.conf</code> file to use as your Upload Dest to RClone.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "TOKEN_PICKLE": (
        "",
        "",
        "<i>Send your <code>token.pickle</code> to use as your Upload Dest to GDrive</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "LEECH_SPLIT_SIZE": (
        "",
        "",
        f"Send Leech split size in bytes or use gb or mb. Example: 40000000 or 2.5gb or 1000mb. PREMIUM_USER: {TgClient.IS_PREMIUM_USER}.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "LEECH_DUMP_CHAT": (
        "",
        "",
        """Send leech destination ID/USERNAME/PM. 
* b:id/@username/pm (b: means leech by bot) (id or username of the chat or write pm means private message so bot will send the files in private to you) when you should use b:(leech by bot)? When your default settings is leech by user and you want to leech by bot for specific task.
* u:id/@username(u: means leech by user) This incase OWNER added USER_STRING_SESSION.
* h:id/@username(hybrid leech) h: to upload files by bot and user based on file size.
* id/@username|topic_id(leech in specific chat and topic) add | without space and write topic id after chat id or username.
┖ <b>Time Left :</b> <code>60 sec</code>""",
    ),
    "LEECH_PREFIX": (
        "",
        "",
        "Send Leech Filename Prefix. You can add HTML tags. Example: <code>@mychannel</code>.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "LEECH_SUFFIX": (
        "",
        "",
        "Send Leech Filename Suffix. You can add HTML tags. Example: <code>@mychannel</code>.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "LEECH_CAPTION": (
        "",
        "",
        "Send Leech Caption. You can add HTML tags and placeholders: <code>{filename}</code> <code>{title}</code> <code>{season}</code> <code>{episode}</code> <code>{size}</code> <code>{duration}</code> <code>{resolution}</code> <code>{quality}</code> <code>{bit}</code> <code>{ott}</code> <code>{lib}</code> <code>{languages}</code> <code>{subtitles}</code>.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "LEECH_FONT": (
        "String",
        "Caption font tag. Supported values: b, i, code, or empty for normal.",
        "Send caption font tag: <code>b</code>, <code>i</code>, <code>code</code>, or <code>none</code>.\n<b>Timeout:</b> 60 sec",
    ),
    "THUMBNAIL_LAYOUT": (
        "",
        "",
        "Send thumbnail layout (widthxheight, 2x2, 3x3, 2x4, 4x4, ...). Example: 3x3.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "SUBTITLE_TRANSLATE_TARGET": (
        "",
        "",
        "Send subtitle translate target language code. Example: en, ta, hi.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "INTRO_SUBTITLE_TEXT": (
        "",
        "",
        "Send intro subtitle text to mux at the start of videos. Send empty text to disable.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "INTRO_SUBTITLE_RANGES": (
        "",
        "Intro subtitle display ranges with fade. The intro track is muxed as the first/default subtitle.",
        "Send ranges like <code>00:00:00 - 00:00:05 | 00:01:20 - 00:01:25</code>.</i> \nâ”– <b>Time Left :</b> <code>60 sec</code>",
    ),
    "AUTO_KEEP_AUDIO_LANGS": (
        "",
        "Audio languages to keep during Auto Remove Streams.",
        "Send language names/codes separated by comma. Example: <code>tam,ta,tamil</code>. Empty means Auto Remove Streams will ask/skip instead of removing audio blindly.</i> \nâ”– <b>Time Left :</b> <code>60 sec</code>",
    ),
    "AUTO_KEEP_SUBTITLE_LANGS": (
        "",
        "Subtitle languages to keep during Auto Remove Streams.",
        "Send language names/codes separated by comma. Example: <code>eng,en,english</code>. Empty means subtitles are not auto-filtered.</i> \nâ”– <b>Time Left :</b> <code>60 sec</code>",
    ),
    "AUTO_AUDIO_ORDER": (
        "",
        "Audio language order for Auto Process.",
        "Send language short codes separated by spaces. Example: <code>tam tel eng</code>.\n<b>Time Left:</b> <code>60 sec</code>",
    ),
    "AUTO_SUBTITLE_ORDER": (
        "",
        "Subtitle language order for Auto Process.",
        "Send subtitle language short codes separated by spaces. Example: <code>eng tam</code>.\n<b>Time Left:</b> <code>60 sec</code>",
    ),
    "RCLONE_PATH": (
        "",
        "",
        "Send Rclone Path. If you want to use your rclone config edit using owner/user config from usetting or add mrcc: before rclone path. Example mrcc:remote:folder. </i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "RCLONE_FLAGS": (
        "",
        "",
        "key:value|key|key|key:value . Check here all <a href='https://rclone.org/flags/'>RcloneFlags</a>\nEx: --buffer-size:8M|--drive-starred-only",
    ),
    "GDRIVE_ID": (
        "",
        "",
        "Send Gdrive ID. If you want to use your token.pickle edit using owner/user token from usetting or add mtp: before the id. Example: mtp:F435RGGRDXXXXXX . </i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "INDEX_URL": (
        "",
        "",
        "Send Index URL for your gdrive option. </i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "UPLOAD_PATHS": (
        "",
        "",
        "Send Dict of keys that have path values. Example: {'path 1': 'remote:rclonefolder', 'path 2': 'gdrive1 id', 'path 3': 'tg chat id', 'path 4': 'mrcc:remote:', 'path 5': b:@username} . </i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "EXCLUDED_EXTENSIONS": (
        "",
        "",
        "Send exluded extenions seperated by space without dot at beginning. </i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "NAME_SWAP": (
        "",
        "",
        """<i>Send your Name Swap. You can add pattern instead of normal text according to the format.</i>
<b>Full Documentation Guide</b> <a href="https://t.me/WZML_X/77">Click Here</a>
┖ <b>Time Left :</b> <code>60 sec</code>
""",
    ),
    "YT_DLP_OPTIONS": (
        "",
        "",
        """Format: {key: value, key: value, key: value}.
Example: {"format": "bv*+mergeall[vcodec=none]", "nocheckcertificate": True, "playliststart": 10, "fragment_retries": float("inf"), "matchtitle": "S13", "writesubtitles": True, "live_from_start": True, "postprocessor_args": {"ffmpeg": ["-threads", "4"]}, "wait_for_video": (5, 100), "download_ranges": [{"start_time": 0, "end_time": 10}], "mx_audio": "all", "mx_quality": ["720", "1080"]}
Check all yt-dlp api options from this <a href='https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/YoutubeDL.py#L184'>FILE</a> or use this <a href='https://t.me/mltb_official_channel/177'>script</a> to convert cli arguments to api options.

<i>Send dict of YT-DLP Options according to format.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>""",
    ),
    "FFMPEG_CMDS": (
        "",
        "",
        """Dict of list values of ffmpeg commands. You can set multiple ffmpeg commands for all files before upload. Don't write ffmpeg at beginning, start directly with the arguments.
Examples: {"subtitle": ["-i mltb.mkv -c copy -c:s srt mltb.mkv", "-i mltb.video -c copy -c:s srt mltb"], "convert": ["-i mltb.m4a -c:a libmp3lame -q:a 2 mltb.mp3", "-i mltb.audio -c:a libmp3lame -q:a 2 mltb.mp3"], extract: ["-i mltb -map 0:a -c copy mltb.mka -map 0:s -c copy mltb.srt"]}
Notes:
- Add `-del` to the list which you want from the bot to delete the original files after command run complete!
- To execute one of those lists in bot for example, you must use -ff subtitle (list key) or -ff convert (list key)
Here I will explain how to use mltb.* which is reference to files you want to work on.
1. First cmd: the input is mltb.mkv so this cmd will work only on mkv videos and the output is mltb.mkv also so all outputs is mkv. -del will delete the original media after complete run of the cmd.
2. Second cmd: the input is mltb.video so this cmd will work on all videos and the output is only mltb so the extenstion is same as input files.
3. Third cmd: the input in mltb.m4a so this cmd will work only on m4a audios and the output is mltb.mp3 so the output extension is mp3.
4. Fourth cmd: the input is mltb.audio so this cmd will work on all audios and the output is mltb.mp3 so the output extension is mp3.

<i>Send dict of FFMPEG_CMDS Options according to format.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>
""",
    ),
    "METADATA_CMDS": (
        "",
        "",
        """<i>Send your Meta data. You can according to the format title="Join @WZML_X".</i>
<b>Full Documentation Guide</b> <a href="https://t.me/WZML_X/">Click Here</a>
┖ <b>Time Left :</b> <code>60 sec</code>
""",
    ),
    "METADATA": (
        "🏷 Global Metadata (key=value|key=value)",
        "Apply metadata to all media files with dynamic variables.",
        """<i>📝 Send metadata as</i> <code>key=value|key2=value2</code>

<b>🔧 Dynamic Variables:</b>
• <code>{filename}</code> - Original filename
• <code>{basename}</code> - Name without extension
• <code>{audiolang}</code> - Audio language (English/Hindi etc.)
• <code>{year}</code> - Year from filename

<b>📋 Example:</b>
<code>title={basename}|artist={audiolang} Version|year={year}</code>

⏱ <b>Time Left:</b> <code>60 sec</code>""",
    ),
    "AUDIO_METADATA": (
        "🎵 Audio Stream Metadata",
        "Metadata applied to each audio track separately.",
        """<i>🎧 Audio stream metadata with per-track language support</i>

<b>📋 Example:</b>
<code>language={audiolang}|title=Audio - {audiolang}</code>

⏱ <b>Time Left:</b> <code>60 sec</code>""",
    ),
    "VIDEO_METADATA": (
        "🎥 Video Stream Metadata",
        "Metadata applied to video streams.",
        """<i>📹 Video stream metadata for visual tracks</i>

<b>📋 Example:</b>
<code>title={basename}|comment=HD Video</code>

⏱ <b>Time Left:</b> <code>60 sec</code>""",
    ),
    "SUBTITLE_METADATA": (
        "💬 Subtitle Stream Metadata",
        "Metadata applied to each subtitle track separately.",
        """<i>📄 Subtitle stream metadata with per-track language support</i>

<b>📋 Example:</b>
<code>language={sublang}|title=Subtitles - {sublang}</code>

⏱ <b>Time Left:</b> <code>60 sec</code>""",
    ),
    "YT_DESP": (
        "String",
        "Custom description for YouTube uploads. Default is used if not set.",
        "<i>Send your custom YouTube description.</i> \nTime Left : <code>60 sec</code>",
    ),
    "YT_TAGS": (
        "Comma-separated strings",
        "Custom tags for YouTube uploads (e.g., tag1,tag2,tag3). Default is used if not set.",
        "<i>Send your custom YouTube tags as a comma-separated list.</i> \nTime Left : <code>60 sec</code>",
    ),
    "YT_CATEGORY_ID": (
        "Number",
        "Custom category ID for YouTube uploads. Default is used if not set.",
        "<i>Send your custom YouTube category ID (e.g., 22).</i> \nTime Left : <code>60 sec</code>",
    ),
    "YT_PRIVACY_STATUS": (
        "public, private, or unlisted",
        "Custom privacy status for YouTube uploads. Default is used if not set.",
        "<i>Send your custom YouTube privacy status (public, private, or unlisted).</i> \nTime Left : <code>60 sec</code>",
    ),
    "USER_COOKIE_FILE": (
        "File",
        "User's YT-DLP Cookie File to authenticate access to websites and youtube.",
        "<i>Send your cookie file (e.g., cookies.txt or abc.txt).</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "GOFILE_TOKEN": (
        "String",
        "Gofile API Token",
        "<i>Send your Gofile API Token.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "GOFILE_FOLDER_ID": (
        "String",
        "Gofile Folder ID",
        "<i>Send your Gofile Folder ID. If empty, uploads to Root.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "BUZZHEAVIER_TOKEN": (
        "String",
        "BuzzHeavier API Token",
        "<i>Send your BuzzHeavier API Token (Account ID).</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "BUZZHEAVIER_FOLDER_ID": (
        "String",
        "BuzzHeavier Folder ID",
        "<i>Send your BuzzHeavier Folder ID.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "PIXELDRAIN_KEY": (
        "String",
        "PixelDrain API Key",
        "<i>Send your PixelDrain API Key.</i> \n┖ <b>Time Left :</b> <code>60 sec</code>",
    ),
    "lremname_auto": (
        "AutoRename Template",
        "AutoRename Template uses filename, caption, media metadata, TMDb, and AniList lookup.",
        "Send AutoRename Template.\n<b>Variables:</b> <code>{file_name} {file_size} {file_caption} {languages} {subtitles} {duration} {ott} {resolution} {name} {title} {year} {quality} {DS4K} {season} {episode} {audio} {lib} {extension} {shortsub} {shortlang} {part} {raw_name} {link} {vcodec} {codec} {acodec} {audio_codec} {audio_channels} {audio_bitrate} {hdr} {dynamic_range} {release_group} {group}</code>\n<b>Offsets:</b> <code>{episode:+12}</code> or <code>{season:-1}</code>\n<b>Example:</b> <code>[S{season}E{episode}] {name} {resolution} {bit} {DS4K} {quality} {codec} {audio_codec} {audio_channels} {hdr}</code>\n<b>Timeout:</b> 60 sec",
    ),
    "lremname_regex": (
        "Regex Remname",
        "Regex Remname uses regex patterns to find and replace parts of filenames.",
        "Send Regex Remname.\n<b>Format:</b> <code>|pattern:replacement|pattern2:replacement2</code>\n<b>Timeout:</b> 60 sec",
    ),
}

user_settings_text["LEECH_CAPTION"] = (
    "",
    "",
    "Send Leech Caption. You can add HTML tags and placeholders: "
    f"<code>{TEMPLATE_VARIABLES_TEXT}</code>.\n"
    "<b>Time Left:</b> <code>60 sec</code>",
)
user_settings_text["lremname_auto"] = (
    "AutoRename Template",
    "AutoRename Template uses filename, caption, media metadata, TMDb, and AniList lookup.",
    "Send AutoRename Template.\n"
    f"<b>Variables:</b> <code>{TEMPLATE_VARIABLES_TEXT}</code>\n"
    "<b>Offsets:</b> <code>{episode:+12}</code> or <code>{season:-1}</code>\n"
    "<b>Example:</b> <code>[S{season}E{episode}] {name} {resolution} {bit} {DS4K} {quality} {codec} {audio_codec} {audio_channels} {hdr}</code>\n"
    "<b>Timeout:</b> 60 sec",
)
for _post_key, _post_title in {
    "POST_MOVIE_CAPTION": "Movie Post Caption",
    "POST_ANIME_CAPTION": "Anime Post Caption",
    "POST_TV_CAPTION": "TV Post Caption",
}.items():
    user_settings_text[_post_key] = (
        "Telegram HTML",
        f"{_post_title} supports Telegram HTML and poster placeholders.",
        "Send caption template.\n"
        "<b>Variables:</b> <code>{name} {title} {year} {season} {episode} {episodes} "
        "{genres} {rating} {status} {plot} {synopsis} {quality} {resolution} "
        "{bit} {codec} {audio} {subtitles} {shortlang} {shortsub}</code>\n"
        "<b>Timeout:</b> 60 sec",
    )
user_settings_text["POST_BRAND_NAME"] = (
    "String",
    "Brand text drawn on generated poster images.",
    "Send poster brand name.\n<b>Timeout:</b> 60 sec",
)
user_settings_text["POST_LOGO"] = (
    "Photo or Doc",
    "Optional logo drawn in the poster corner.",
    "Send a photo/document logo image.\n<b>Timeout:</b> 60 sec",
)


async def get_user_settings(from_user, stype="main"):
    user_id = from_user.id
    user_name = from_user.mention(style="html")
    buttons = ButtonMaker()
    rclone_conf = f"rclone/{user_id}.conf"
    token_pickle = f"tokens/{user_id}.pickle"
    user_dict = user_data.get(user_id, {})

    if stype == "main":
        buttons.data_button(
            "General Settings", f"userset {user_id} general", position="header"
        )
        buttons.data_button("Mirror Settings", f"userset {user_id} mirror")
        buttons.data_button("Leech Settings", f"userset {user_id} leech")
        buttons.data_button("Post Settings", f"userset {user_id} post")
        buttons.data_button("Auto Process", f"userset {user_id} autoprocess")
        buttons.data_button("Metadata", f"userset {user_id} ffset")
        buttons.data_button("User Settings Zip", f"userset {user_id} zip")
        buttons.data_button("Import Settings Zip", f"userset {user_id} zipimport")
        buttons.data_button(
            "Mics Settings", f"userset {user_id} advanced", position="l_body"
        )

        if user_dict and any(
            key in user_dict
            for key in list(user_settings_text.keys())
            + [
                "USER_TOKENS",
                "AS_DOCUMENT",
                "EQUAL_SPLITS",
                "MEDIA_GROUP",
                "USER_TRANSMISSION",
                "HYBRID_LEECH",
                "STOP_DUPLICATE",
                "DEFAULT_UPLOAD",
                "AUTO_POSTER_ENABLED",
                "AUTO_POSTER_USE_AS_THUMBNAIL",
                "POST_TEMPLATE_ID",
            ]
        ):
            buttons.data_button(
                f"{RED_DOT} Reset All",
                f"userset {user_id} confirm_reset_all",
                position="footer",
                style=ButtonStyle.DANGER,
            )
        buttons.data_button(
            f"{RED_DOT} Close",
            f"userset {user_id} close",
            position="footer",
            style=ButtonStyle.DANGER,
        )

        text = f"""⌬ <b>User Settings :</b>
│
┟ <b>Name</b> → {user_name}
┠ <b>UserID</b> → #ID{user_id}
┠ <b>Username</b> → @{from_user.username}
┠ <b>Telegram DC</b> → {from_user.dc_id}
┖ <b>Telegram Lang</b> → {Language.get(lc).display_name() if (lc := from_user.language_code) else "N/A"}"""

        btns = buttons.build_menu(2)

    elif stype == "general":
        if user_dict.get("DEFAULT_UPLOAD", ""):
            default_upload = user_dict["DEFAULT_UPLOAD"]
        elif "DEFAULT_UPLOAD" not in user_dict:
            default_upload = Config.DEFAULT_UPLOAD
        du = "GDRIVE API" if default_upload == "gd" else "RCLONE"
        dur = "GDRIVE API" if default_upload != "gd" else "RCLONE"
        buttons.data_button(
            f"Swap to {dur} Mode", f"userset {user_id} {default_upload}"
        )

        user_tokens = user_dict.get("USER_TOKENS", False)
        tr = "USER" if user_tokens else "OWNER"
        trr = "OWNER" if user_tokens else "USER"
        buttons.data_button(
            f"Swap to {trr} token/config",
            f"userset {user_id} tog USER_TOKENS {'f' if user_tokens else 't'}",
        )
        buttons.data_button("Helper Token Settings", f"userset {user_id} userbot")

        buttons.data_button("Back", f"userset {user_id} back", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")

        def_cookies = user_dict.get("USE_DEFAULT_COOKIE", False)
        cookie_mode = "Owner's Cookie" if def_cookies else "User's Cookie"
        buttons.data_button(
            f"Swap to {'OWNER' if not def_cookies else 'USER'}'s Cookie File",
            f"userset {user_id} tog USE_DEFAULT_COOKIE {'f' if def_cookies else 't'}",
        )
        btns = buttons.build_menu(1)

        text = f"""⌬ <b>General Settings :</b>
┟ <b>Name</b> → {user_name}
┃
┠ <b>Default Upload Package</b> → <b>{du}</b>
┠ <b>Default Usage Mode</b> → <b>{tr}'s</b> token/config
┖ <b>yt Cookies Mode</b> → <b>{cookie_mode}</b>
"""

    elif stype == "userbot":
        pin_required = config_bool(Config.HELPER_TOKEN_PIN_REQUIRED, True)
        pin_set = starfallx_upload.pin_is_set(user_id)
        unlocked = starfallx_upload.pin_unlocked(user_id)
        if pin_required and not pin_set:
            buttons.data_button("Set PIN", f"userset {user_id} helperpinset")
            text = """<b>StarFallX Helper Token Settings</b>

Set a PIN before adding helper bot tokens.
Tokens are masked in settings and backup zip."""
        elif pin_required and not unlocked:
            buttons.data_button("Unlock PIN", f"userset {user_id} helperunlock")
            text = """<b>StarFallX Helper Token Settings</b>

PIN lock is enabled.
Unlock to view, add, test, remove, or export helper token data."""
        else:
            buttons.data_button("Token List", f"userset {user_id} userbot_tokens")
            buttons.data_button("Backup Token List", f"userset {user_id} userbot_backups")
            buttons.data_button("Add Token", f"userset {user_id} helperadd")
            buttons.data_button("Test Tokens", f"userset {user_id} helpertest")
            buttons.data_button("Remove Token", f"userset {user_id} helperremove")
            buttons.data_button("Set Primary", f"userset {user_id} helpersetprimary")
            buttons.data_button("Token Status", f"userset {user_id} userbot_status")
            if pin_required:
                buttons.data_button("Lock PIN", f"userset {user_id} helperlock")
            text = starfallx_upload.format_user_status(user_id)
        buttons.data_button("Back", f"userset {user_id} general", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        btns = buttons.build_menu(1)

    elif stype in {"userbot_tokens", "userbot_backups", "userbot_status"}:
        records = starfallx_upload.get_user_token_records(user_id)
        if stype == "userbot_tokens":
            selected = [record for record in records if record.get("primary")] or records[:1]
            title = "Helper Token List"
        elif stype == "userbot_backups":
            selected = [record for record in records if not record.get("primary")]
            title = "Backup Helper Tokens"
        else:
            selected = records
            title = "Helper Token Status"

        lines = [f"<b>{title}</b>", ""]
        if not selected:
            lines.append("No helper tokens saved in this list.")
        else:
            for index, record in enumerate(selected, start=1):
                username = record.get("username") or "Not Tested"
                status = record.get("status") or "Not Tested"
                primary = "Primary" if record.get("primary") else "Backup"
                lines.append(
                    f"{index}. @{escape(str(username))} - "
                    f"<code>{escape(str(record.get('mask') or 'Masked'))}</code>"
                )
                lines.append(f"   {primary} | {escape(str(status))}")
                if record.get("last_error") and status not in {"Ready", "Direct Only"}:
                    lines.append(f"   Error: <code>{escape(str(record.get('last_error'))[:140])}</code>")
        text = "\n".join(lines)
        buttons.data_button("Back", f"userset {user_id} userbot", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        btns = buttons.build_menu(1)

    elif stype == "leech":
        def enabled(key):
            return bool(
                user_dict.get(key, False)
                or key not in user_dict
                and getattr(Config, key, False)
            )

        def tick(key):
            return "✅ " if enabled(key) else ""

        thumbpath = f"thumbnails/{user_id}.jpg"
        thumbmsg = "Exists" if await aiopath.exists(thumbpath) else "Not Exists"
        split_size = user_dict.get("LEECH_SPLIT_SIZE") or Config.LEECH_SPLIT_SIZE
        leech_dest = (
            user_dict.get("LEECH_DUMP_CHAT")
            or (Config.LEECH_DUMP_CHAT if "LEECH_DUMP_CHAT" not in user_dict else "")
            or "None"
        )
        lprefix = (
            user_dict.get("LEECH_PREFIX")
            or (Config.LEECH_PREFIX if "LEECH_PREFIX" not in user_dict else "")
            or "Not Exists"
        )
        lsuffix = (
            user_dict.get("LEECH_SUFFIX")
            or (Config.LEECH_SUFFIX if "LEECH_SUFFIX" not in user_dict else "")
            or "Not Exists"
        )
        lcap = (
            user_dict.get("LEECH_CAPTION")
            or (Config.LEECH_CAPTION if "LEECH_CAPTION" not in user_dict else "")
            or "Not Exists"
        )
        thumb_layout = (
            user_dict.get("THUMBNAIL_LAYOUT")
            or (Config.THUMBNAIL_LAYOUT if "THUMBNAIL_LAYOUT" not in user_dict else "")
            or "None"
        )
        lremname_auto = (
            user_dict.get("lremname_auto")
            or Config.LEECH_FILENAME_REMNAME_AUTO
            or "Not Set"
        )
        subtitle_target = (
            user_dict.get("SUBTITLE_TRANSLATE_TARGET")
            or Config.SUBTITLE_TRANSLATE_TARGET
            or "en"
        )
        intro_subtitle = (
            user_dict.get("INTRO_SUBTITLE_TEXT")
            or Config.INTRO_SUBTITLE_TEXT
            or "Not Set"
        )
        font_value = user_dict.get("LEECH_FONT", Config.LEECH_FONT) or ""
        font_label = {"": "Normal", "b": "Bold", "i": "Italic"}.get(font_value, font_value)
        ltype = "DOCUMENT" if enabled("AS_DOCUMENT") else "MEDIA"
        auto_thumb = "Enabled" if enabled("AUTO_THUMBNAIL") else "Disabled"
        autorename_status = "Enabled" if enabled("AUTORENAME") else "Disabled"
        complete_msg = "Enabled" if enabled("LEECH_COMPLETE_MSG") else "Disabled"
        sequential_leech = "Enabled" if enabled("SEQUENTIAL_LEECH") else "Disabled"
        premium_status = "Yes" if TgClient.IS_PREMIUM_USER else "No"
        user_upload = bool(TgClient.IS_PREMIUM_USER and enabled("USER_TRANSMISSION"))
        hybrid_upload = bool(TgClient.IS_PREMIUM_USER and enabled("HYBRID_LEECH"))
        premium_upload_enabled = user_upload or hybrid_upload
        upload_mode = "Hybrid" if hybrid_upload else ("User" if user_upload else "Bot")
        effective_split = (
            TgClient.MAX_SPLIT_SIZE if premium_upload_enabled else 2097152000
        )

        buttons.data_button("Thumbnail", f"userset {user_id} menu THUMBNAIL")
        buttons.data_button("Leech Split Size", f"userset {user_id} menu LEECH_SPLIT_SIZE")
        buttons.data_button("Leech Destination", f"userset {user_id} menu LEECH_DUMP_CHAT")
        buttons.data_button("Leech Prefix", f"userset {user_id} menu LEECH_PREFIX")
        buttons.data_button("Leech Suffix", f"userset {user_id} menu LEECH_SUFFIX")
        buttons.data_button("Leech Caption", f"userset {user_id} menu LEECH_CAPTION")
        buttons.data_button("Thumbnail Layout", f"userset {user_id} menu THUMBNAIL_LAYOUT")
        buttons.data_button(
            state_label("Send As Document", enabled("AS_DOCUMENT")),
            f"userset {user_id} tog AS_DOCUMENT {'f' if enabled('AS_DOCUMENT') else 't'}",
            style=state_style(enabled("AS_DOCUMENT")),
        )
        if TgClient.IS_PREMIUM_USER:
            buttons.data_button(
                state_label("Leech by User" if user_upload else "Leech by Bot", user_upload),
                f"userset {user_id} tog USER_TRANSMISSION {'f' if user_upload else 't'}",
                style=state_style(user_upload),
            )
            buttons.data_button(
                state_label("Hybrid Leech", hybrid_upload),
                f"userset {user_id} tog HYBRID_LEECH {'f' if hybrid_upload else 't'}",
                style=state_style(hybrid_upload),
            )
        buttons.data_button(
            state_label("Auto Thumbnail", enabled("AUTO_THUMBNAIL")),
            f"userset {user_id} tog AUTO_THUMBNAIL {'f' if enabled('AUTO_THUMBNAIL') else 't'}",
            style=state_style(enabled("AUTO_THUMBNAIL")),
        )
        buttons.data_button(
            state_label("AutoRename", enabled("AUTORENAME")),
            f"userset {user_id} tog AUTORENAME {'f' if enabled('AUTORENAME') else 't'}",
            style=state_style(enabled("AUTORENAME")),
        )
        buttons.data_button("AutoRename Template", f"userset {user_id} menu lremname_auto")
        buttons.data_button(
            state_label("Complete Msg", enabled("LEECH_COMPLETE_MSG")),
            f"userset {user_id} tog LEECH_COMPLETE_MSG {'f' if enabled('LEECH_COMPLETE_MSG') else 't'}",
            style=state_style(enabled("LEECH_COMPLETE_MSG")),
        )
        buttons.data_button(
            state_label("Sequential Leech", enabled("SEQUENTIAL_LEECH")),
            f"userset {user_id} tog SEQUENTIAL_LEECH {'f' if enabled('SEQUENTIAL_LEECH') else 't'}",
            style=state_style(enabled("SEQUENTIAL_LEECH")),
        )
        buttons.data_button(f"Caption Font ({font_label})", f"userset {user_id} font")
        buttons.data_button("Subtitle Target", f"userset {user_id} menu SUBTITLE_TRANSLATE_TARGET")
        buttons.data_button("Intro Subtitle", f"userset {user_id} menu INTRO_SUBTITLE_TEXT")
        buttons.data_button("Back", f"userset {user_id} back", "footer")
        buttons.data_button(f"{RED_DOT} Close", f"userset {user_id} close", "footer", style=ButtonStyle.DANGER)
        btns = buttons.build_menu(2)

        text = f"""<b>Leech Settings</b>
Name: {user_name}

Leech Type: <b>{ltype}</b>
Premium User: <b>{premium_status}</b>
Upload Mode: <b>{upload_mode}</b>
Max Split Size: <b>{get_readable_file_size(effective_split)}</b>
Custom Thumbnail: <b>{thumbmsg}</b>
Leech Split Size: <b>{get_readable_file_size(split_size)}</b>
Leech Destination: <code>{escape(str(leech_dest))}</code>
Leech Prefix: <code>{escape(lprefix)}</code>
Leech Suffix: <code>{escape(lsuffix)}</code>
Leech Caption: <code>{escape(lcap)}</code>
Caption Font: <b>{font_label}</b>
Complete Msg: <b>{complete_msg}</b>
Sequential Leech: <b>{sequential_leech}</b>
Thumbnail Layout: <b>{thumb_layout}</b>
Auto Thumbnail: <b>{auto_thumb}</b>
AutoRename: <b>{autorename_status}</b>
AutoRename Template: <code>{escape(lremname_auto)}</code>
Subtitle Target: <code>{escape(subtitle_target)}</code>
Intro Subtitle: <code>{escape(intro_subtitle)}</code>
"""

    elif stype == "leech_old":
        thumbpath = f"thumbnails/{user_id}.jpg"
        buttons.data_button("Thumbnail", f"userset {user_id} menu THUMBNAIL")
        thumbmsg = "Exists" if await aiopath.exists(thumbpath) else "Not Exists"
        buttons.data_button(
            "Leech Split Size", f"userset {user_id} menu LEECH_SPLIT_SIZE"
        )
        if user_dict.get("LEECH_SPLIT_SIZE", False):
            split_size = user_dict["LEECH_SPLIT_SIZE"]
        else:
            split_size = Config.LEECH_SPLIT_SIZE
        buttons.data_button(
            "Leech Destination", f"userset {user_id} menu LEECH_DUMP_CHAT"
        )
        if user_dict.get("LEECH_DUMP_CHAT", False):
            leech_dest = user_dict["LEECH_DUMP_CHAT"]
        elif "LEECH_DUMP_CHAT" not in user_dict and Config.LEECH_DUMP_CHAT:
            leech_dest = Config.LEECH_DUMP_CHAT
        else:
            leech_dest = "None"
        buttons.data_button("Leech Prefix", f"userset {user_id} menu LEECH_PREFIX")
        if user_dict.get("LEECH_PREFIX", False):
            lprefix = user_dict["LEECH_PREFIX"]
        elif "LEECH_PREFIX" not in user_dict and Config.LEECH_PREFIX:
            lprefix = Config.LEECH_PREFIX
        else:
            lprefix = "Not Exists"
        buttons.data_button("Leech Suffix", f"userset {user_id} menu LEECH_SUFFIX")
        if user_dict.get("LEECH_SUFFIX", False):
            lsuffix = user_dict["LEECH_SUFFIX"]
        elif "LEECH_SUFFIX" not in user_dict and Config.LEECH_SUFFIX:
            lsuffix = Config.LEECH_SUFFIX
        else:
            lsuffix = "Not Exists"

        buttons.data_button("Leech Caption", f"userset {user_id} menu LEECH_CAPTION")
        if user_dict.get("LEECH_CAPTION", False):
            lcap = user_dict["LEECH_CAPTION"]
        elif "LEECH_CAPTION" not in user_dict and Config.LEECH_CAPTION:
            lcap = Config.LEECH_CAPTION
        else:
            lcap = "Not Exists"

        if (
            user_dict.get("AS_DOCUMENT", False)
            or "AS_DOCUMENT" not in user_dict
            and Config.AS_DOCUMENT
        ):
            ltype = "DOCUMENT"
            buttons.data_button("Send As Media", f"userset {user_id} tog AS_DOCUMENT f")
        else:
            ltype = "MEDIA"
            buttons.data_button(
                "Send As Document", f"userset {user_id} tog AS_DOCUMENT t"
            )
        if (
            user_dict.get("EQUAL_SPLITS", False)
            or "EQUAL_SPLITS" not in user_dict
            and Config.EQUAL_SPLITS
        ):
            buttons.data_button(
                "Disable Equal Splits", f"userset {user_id} tog EQUAL_SPLITS f"
            )
            equal_splits = "Enabled"
        else:
            buttons.data_button(
                "Enable Equal Splits", f"userset {user_id} tog EQUAL_SPLITS t"
            )
            equal_splits = "Disabled"
        if (
            user_dict.get("MEDIA_GROUP", False)
            or "MEDIA_GROUP" not in user_dict
            and Config.MEDIA_GROUP
        ):
            buttons.data_button(
                "Disable Media Group", f"userset {user_id} tog MEDIA_GROUP f"
            )
            media_group = "Enabled"
        else:
            buttons.data_button(
                "Enable Media Group", f"userset {user_id} tog MEDIA_GROUP t"
            )
            media_group = "Disabled"
        if (
            TgClient.IS_PREMIUM_USER
            and user_dict.get("USER_TRANSMISSION", False)
            or "USER_TRANSMISSION" not in user_dict
            and Config.USER_TRANSMISSION
        ):
            buttons.data_button(
                "Leech by Bot", f"userset {user_id} tog USER_TRANSMISSION f"
            )
            leech_method = "user"
        elif TgClient.IS_PREMIUM_USER:
            leech_method = "bot"
            buttons.data_button(
                "Leech by User", f"userset {user_id} tog USER_TRANSMISSION t"
            )
        else:
            leech_method = "bot"

        if (
            TgClient.IS_PREMIUM_USER
            and user_dict.get("HYBRID_LEECH", False)
            or "HYBRID_LEECH" not in user_dict
            and Config.HYBRID_LEECH
        ):
            hybrid_leech = "Enabled"
            buttons.data_button(
                "Disable Hybride Leech", f"userset {user_id} tog HYBRID_LEECH f"
            )
        elif TgClient.IS_PREMIUM_USER:
            hybrid_leech = "Disabled"
            buttons.data_button(
                "Enable HYBRID Leech", f"userset {user_id} tog HYBRID_LEECH t"
            )
        else:
            hybrid_leech = "Disabled"

        buttons.data_button(
            "Thumbnail Layout", f"userset {user_id} menu THUMBNAIL_LAYOUT"
        )
        if user_dict.get("THUMBNAIL_LAYOUT", False):
            thumb_layout = user_dict["THUMBNAIL_LAYOUT"]
        elif "THUMBNAIL_LAYOUT" not in user_dict and Config.THUMBNAIL_LAYOUT:
            thumb_layout = Config.THUMBNAIL_LAYOUT
        else:
            thumb_layout = "None"

        # Auto Thumbnail toggle
        if (
            user_dict.get("AUTO_THUMBNAIL", False)
            or "AUTO_THUMBNAIL" not in user_dict
            and Config.AUTO_THUMBNAIL
        ):
            buttons.data_button(
                "Disable Auto Thumbnail", f"userset {user_id} tog AUTO_THUMBNAIL f"
            )
            auto_thumb = "Enabled"
        else:
            buttons.data_button(
                "Enable Auto Thumbnail", f"userset {user_id} tog AUTO_THUMBNAIL t"
            )
            auto_thumb = "Disabled"

        # AutoRename toggle
        if (
            user_dict.get("AUTORENAME", False)
            or "AUTORENAME" not in user_dict
            and Config.AUTORENAME
        ):
            buttons.data_button(
                "Disable AutoRename", f"userset {user_id} tog AUTORENAME f"
            )
            autorename_status = "Enabled"
        else:
            buttons.data_button(
                "Enable AutoRename", f"userset {user_id} tog AUTORENAME t"
            )
            autorename_status = "Disabled"

        # Rename Method toggle
        rename_method = user_dict.get("RENAME_METHOD") or Config.RENAME_METHOD
        if rename_method == "auto":
            buttons.data_button(
                "Switch to Regex", f"userset {user_id} tog RENAME_METHOD regex"
            )
        else:
            buttons.data_button(
                "Switch to Auto", f"userset {user_id} tog RENAME_METHOD auto"
            )

        # AutoRename Template
        buttons.data_button(
            "AutoRename Template", f"userset {user_id} menu lremname_auto"
        )
        lremname_auto = user_dict.get("lremname_auto") or Config.LEECH_FILENAME_REMNAME_AUTO or "Not Set"

        # Regex Remname
        buttons.data_button(
            "Regex Remname", f"userset {user_id} menu lremname_regex"
        )
        lremname_regex = user_dict.get("lremname_regex") or Config.LEECH_FILENAME_REMNAME_REGEX or "Not Set"

        buttons.data_button(
            "Subtitle Target", f"userset {user_id} menu SUBTITLE_TRANSLATE_TARGET"
        )
        subtitle_target = user_dict.get("SUBTITLE_TRANSLATE_TARGET") or Config.SUBTITLE_TRANSLATE_TARGET or "en"

        buttons.data_button(
            "Intro Subtitle", f"userset {user_id} menu INTRO_SUBTITLE_TEXT"
        )
        intro_subtitle = user_dict.get("INTRO_SUBTITLE_TEXT") or Config.INTRO_SUBTITLE_TEXT or "Not Set"
        buttons.data_button("Back", f"userset {user_id} back", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        btns = buttons.build_menu(2)

        text = f"""⌬ <b>Leech Settings :</b>
┟ <b>Name</b> → {user_name}
┃
┠ Leech Type → <b>{ltype}</b>
┠ Custom Thumbnail → <b>{thumbmsg}</b>
┠ Leech Split Size → <b>{get_readable_file_size(split_size)}</b>
┠ Equal Splits → <b>{equal_splits}</b>
┠ Media Group → <b>{media_group}</b>
┠ Leech Prefix → <code>{escape(lprefix)}</code>
┠ Leech Suffix → <code>{escape(lsuffix)}</code>
┠ Leech Caption → <code>{escape(lcap)}</code>
┠ Leech Destination → <code>{leech_dest}</code>
┠ Leech by <b>{leech_method}</b> session
┠ Mixed Leech → <b>{hybrid_leech}</b>
┠ Thumbnail Layout → <b>{thumb_layout}</b>
┠ Auto Thumbnail → <b>{auto_thumb}</b>
┠ AutoRename → <b>{autorename_status}</b>
┠ Rename Method → <b>{rename_method}</b>
┠ AutoRename Template → <code>{escape(lremname_auto)}</code>
┠ Regex Remname → <code>{escape(lremname_regex)}</code>
┠ Subtitle Target → <code>{escape(subtitle_target)}</code>
┖ Intro Subtitle → <code>{escape(intro_subtitle)}</code>
"""

    elif stype == "post":
        def enabled(key, default=False):
            value = user_dict.get(key) if key in user_dict else getattr(Config, key, default)
            return config_bool(value, default)

        def tick(key, default=False):
            return f"{CHECK_MARK} " if enabled(key, default) else f"{CROSS_MARK} "

        template_id = str(user_dict.get("POST_TEMPLATE_ID") or Config.POST_TEMPLATE_ID or 1)
        if template_id not in {str(i) for i in range(1, 9)}:
            template_id = "1"
        brand = user_dict.get("POST_BRAND_NAME") or Config.POST_BRAND_NAME or "Anime Starfall"
        logo = user_dict.get("POST_LOGO") or Config.POST_LOGO or ""
        logo_msg = "Exists" if logo and (str(logo).startswith(("http://", "https://")) or await aiopath.exists(str(logo))) else "Not Exists"

        buttons.data_button(
            state_label("Auto Poster", enabled("AUTO_POSTER_ENABLED")),
            f"userset {user_id} tog AUTO_POSTER_ENABLED {'f' if enabled('AUTO_POSTER_ENABLED') else 't'}",
            style=state_style(enabled("AUTO_POSTER_ENABLED")),
        )
        buttons.data_button(
            state_label("Use As Thumbnail", enabled("AUTO_POSTER_USE_AS_THUMBNAIL", True)),
            f"userset {user_id} tog AUTO_POSTER_USE_AS_THUMBNAIL {'f' if enabled('AUTO_POSTER_USE_AS_THUMBNAIL', True) else 't'}",
            style=state_style(enabled("AUTO_POSTER_USE_AS_THUMBNAIL", True)),
        )
        buttons.data_button("Template", f"userset {user_id} posttemplate")
        buttons.data_button("Movie Caption", f"userset {user_id} menu POST_MOVIE_CAPTION")
        buttons.data_button("Anime Caption", f"userset {user_id} menu POST_ANIME_CAPTION")
        buttons.data_button("TV Caption", f"userset {user_id} menu POST_TV_CAPTION")
        buttons.data_button("Brand Name", f"userset {user_id} menu POST_BRAND_NAME")
        buttons.data_button("Logo", f"userset {user_id} menu POST_LOGO")
        buttons.data_button("Back", f"userset {user_id} back", "footer")
        buttons.data_button(f"{RED_DOT} Close", f"userset {user_id} close", "footer", style=ButtonStyle.DANGER)
        btns = buttons.build_menu(2)

        text = f"""<b>Post Settings</b>
Name: {user_name}

Auto Poster: <b>{'Enabled' if enabled('AUTO_POSTER_ENABLED') else 'Disabled'}</b>
Use Poster As Thumbnail: <b>{'Enabled' if enabled('AUTO_POSTER_USE_AS_THUMBNAIL', True) else 'Disabled'}</b>
Template: <b>{template_id}/8</b>
Brand: <code>{escape(str(brand))}</code>
Logo: <b>{logo_msg}</b>

Use /poster or /p to manually search and save a thumbnail style.
"""

    elif stype == "posttemplate":
        template_id = str(user_dict.get("POST_TEMPLATE_ID") or Config.POST_TEMPLATE_ID or 1)
        for i in range(1, 9):
            prefix = f"{GREEN_DOT} " if template_id == str(i) else ""
            buttons.data_button(
                f"{prefix}Template {i}",
                f"userset {user_id} posttemplateset {i}",
                style=ButtonStyle.SUCCESS if template_id == str(i) else None,
            )
        buttons.data_button("Back", f"userset {user_id} post", "footer")
        buttons.data_button(f"{RED_DOT} Close", f"userset {user_id} close", "footer", style=ButtonStyle.DANGER)
        btns = buttons.build_menu(2)
        text = f"""<b>Poster Template</b>

Choose one of the six stable 1280x720 poster styles.
Current: <b>{escape(template_id)}</b>"""

    elif stype == "autoprocess":
        def enabled(key):
            return bool(
                user_dict.get(key, False)
                or key not in user_dict
                and getattr(Config, key, False)
            )

        def value_set(key):
            value = user_dict.get(key)
            if value is None:
                value = getattr(Config, key, "")
            return bool(str(value or "").strip())

        toggles = [
            ("AUTO_PROCESS", "Auto Process"),
            ("AUTO_LEECH", "Auto Leech"),
            ("AUTO_UNZIP", "Auto Unzip"),
            ("AUTO_VT", "Auto -vt"),
            ("AUTO_ORDER", "Auto Order"),
            ("AUTO_REMOVE_STREAMS", "Auto Remove Streams"),
            ("AUTO_MERGE", "Auto Merge"),
            ("AUTO_INTRO_SUBTITLE", "Intro Subtitle"),
            ("AUTO_METADATA", "Metadata"),
        ]
        status_lines = []
        for key, label in toggles:
            state = enabled(key)
            buttons.data_button(
                state_label(label, state),
                f"userset {user_id} tog {key} {'f' if state else 't'}",
                style=state_style(state),
            )
            status_lines.append(f"- {label}: <b>{'On' if state else 'Off'}</b>")

        buttons.data_button(
            state_label("Keep Audios", value_set("AUTO_KEEP_AUDIO_LANGS")),
            f"userset {user_id} menu AUTO_KEEP_AUDIO_LANGS",
            style=state_style(value_set("AUTO_KEEP_AUDIO_LANGS")),
        )
        buttons.data_button(
            state_label("Keep Subtitles", value_set("AUTO_KEEP_SUBTITLE_LANGS")),
            f"userset {user_id} menu AUTO_KEEP_SUBTITLE_LANGS",
            style=state_style(value_set("AUTO_KEEP_SUBTITLE_LANGS")),
        )
        buttons.data_button(
            state_label("Audios Order", value_set("AUTO_AUDIO_ORDER")),
            f"userset {user_id} menu AUTO_AUDIO_ORDER",
            style=state_style(value_set("AUTO_AUDIO_ORDER")),
        )
        buttons.data_button(
            state_label("Subtitles Order", value_set("AUTO_SUBTITLE_ORDER")),
            f"userset {user_id} menu AUTO_SUBTITLE_ORDER",
            style=state_style(value_set("AUTO_SUBTITLE_ORDER")),
        )
        buttons.data_button(
            state_label("Intro Ranges", value_set("INTRO_SUBTITLE_RANGES")),
            f"userset {user_id} menu INTRO_SUBTITLE_RANGES",
            style=state_style(value_set("INTRO_SUBTITLE_RANGES")),
        )

        keep_audio = user_dict.get("AUTO_KEEP_AUDIO_LANGS") or Config.AUTO_KEEP_AUDIO_LANGS or "Not Set"
        keep_sub = user_dict.get("AUTO_KEEP_SUBTITLE_LANGS") or Config.AUTO_KEEP_SUBTITLE_LANGS or "Not Set"
        audio_order = user_dict.get("AUTO_AUDIO_ORDER") or Config.AUTO_AUDIO_ORDER or "Default"
        sub_order = user_dict.get("AUTO_SUBTITLE_ORDER") or Config.AUTO_SUBTITLE_ORDER or "Default"
        intro_ranges = user_dict.get("INTRO_SUBTITLE_RANGES") or Config.INTRO_SUBTITLE_RANGES or "Not Set"

        buttons.data_button("Back", f"userset {user_id} back", "footer")
        buttons.data_button(f"{RED_DOT} Close", f"userset {user_id} close", "footer", style=ButtonStyle.DANGER)
        btns = buttons.build_menu(2)

        text = f"""<b>Auto Process</b>
<b>Name:</b> {user_name}

{chr(10).join(status_lines)}

Keep Audios -> <code>{escape(str(keep_audio))}</code>
Keep Subtitles -> <code>{escape(str(keep_sub))}</code>
Audios Order -> <code>{escape(str(audio_order))}</code>
Subtitles Order -> <code>{escape(str(sub_order))}</code>
Intro Ranges -> <code>{escape(str(intro_ranges))}</code>

<i>Order: download - unzip - order tracks - remove streams - smart merge - intro subtitle - metadata - auto rename - sequential upload.</i>
"""

    elif stype == "uphoster":
        uphoster_service = user_dict.get("UPHOSTER_SERVICE", "gofile")
        buttons.data_button(
            "Change Destination ⇋",
            f"userset {user_id} uphoster_destinations",
        )
        buttons.data_button("Gofile Tools", f"userset {user_id} gofile")
        buttons.data_button("BuzzHeavier Tools", f"userset {user_id} buzzheavier")
        buttons.data_button("PixelDrain Tools", f"userset {user_id} pixeldrain")
        buttons.data_button("Back", f"userset {user_id} back", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        btns = buttons.build_menu(1)

        destinations = [s.capitalize() for s in uphoster_service.split(",")]
        text = f"""⌬ <b>Uphoster Settings :</b>
┟ <b>Name</b> → {user_name}
┃
┖ <b>Current Destination</b> → {', '.join(destinations)}"""

    elif stype == "pixeldrain":
        buttons.data_button("PixelDrain Key", f"userset {user_id} menu PIXELDRAIN_KEY")
        buttons.data_button("Back", f"userset {user_id} back uphoster", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        btns = buttons.build_menu(1)

        if user_dict.get("PIXELDRAIN_KEY", False):
            pdtoken = user_dict["PIXELDRAIN_KEY"]
        elif Config.PIXELDRAIN_KEY:
            pdtoken = Config.PIXELDRAIN_KEY
        else:
            pdtoken = "None"

        text = f"""⌬ <b>PixelDrain Settings :</b>
┟ <b>Name</b> → {user_name}
┃
┖ <b>PixelDrain Key</b> → <code>{pdtoken}</code>"""

    elif stype == "buzzheavier":
        buttons.data_button(
            "BuzzHeavier Token", f"userset {user_id} menu BUZZHEAVIER_TOKEN"
        )
        buttons.data_button(
            "BuzzHeavier Folder ID", f"userset {user_id} menu BUZZHEAVIER_FOLDER_ID"
        )
        buttons.data_button("Back", f"userset {user_id} back uphoster", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        btns = buttons.build_menu(1)

        if user_dict.get("BUZZHEAVIER_TOKEN", False):
            bztoken = user_dict["BUZZHEAVIER_TOKEN"]
        elif Config.BUZZHEAVIER_API:
            bztoken = Config.BUZZHEAVIER_API
        else:
            bztoken = "None"

        if user_dict.get("BUZZHEAVIER_FOLDER_ID", False):
            bzfolder = user_dict["BUZZHEAVIER_FOLDER_ID"]
        else:
            bzfolder = "None"

        text = f"""⌬ <b>BuzzHeavier Settings :</b>
┟ <b>Name</b> → {user_name}
┃
┠ <b>BuzzHeavier Token</b> → <code>{bztoken}</code>
┖ <b>BuzzHeavier Folder ID</b> → <code>{bzfolder}</code>"""

    elif stype == "gofile":
        buttons.data_button("Gofile Token", f"userset {user_id} menu GOFILE_TOKEN")
        buttons.data_button(
            "Gofile Folder ID", f"userset {user_id} menu GOFILE_FOLDER_ID"
        )
        buttons.data_button("Back", f"userset {user_id} back uphoster", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        btns = buttons.build_menu(1)

        if user_dict.get("GOFILE_TOKEN", False):
            gftoken = user_dict["GOFILE_TOKEN"]
        elif Config.GOFILE_API:
            gftoken = Config.GOFILE_API
        else:
            gftoken = "None"

        if user_dict.get("GOFILE_FOLDER_ID", False):
            gffolder = user_dict["GOFILE_FOLDER_ID"]
        elif Config.GOFILE_FOLDER_ID:
            gffolder = Config.GOFILE_FOLDER_ID
        else:
            gffolder = "None (Uploads to Root)"

        text = f"""⌬ <b>Gofile Settings :</b>
┟ <b>Name</b> → {user_name}
┃
┠ <b>Gofile Token</b> → <code>{gftoken}</code>
┖ <b>Gofile Folder ID</b> → <code>{gffolder}</code>"""

    elif stype == "rclone":
        buttons.data_button("Rclone Config", f"userset {user_id} menu RCLONE_CONFIG")
        buttons.data_button(
            "Default Rclone Path", f"userset {user_id} menu RCLONE_PATH"
        )
        buttons.data_button("Rclone Flags", f"userset {user_id} menu RCLONE_FLAGS")

        buttons.data_button("Back", f"userset {user_id} back mirror", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")

        rccmsg = "Exists" if await aiopath.exists(rclone_conf) else "Not Exists"
        if user_dict.get("RCLONE_PATH", False):
            rccpath = user_dict["RCLONE_PATH"]
        elif Config.RCLONE_PATH:
            rccpath = Config.RCLONE_PATH
        else:
            rccpath = "None"
        btns = buttons.build_menu(1)

        if user_dict.get("RCLONE_FLAGS", False):
            rcflags = user_dict["RCLONE_FLAGS"]
        elif "RCLONE_FLAGS" not in user_dict and Config.RCLONE_FLAGS:
            rcflags = Config.RCLONE_FLAGS
        else:
            rcflags = "None"

        text = f"""⌬ <b>RClone Settings :</b>
┟ <b>Name</b> → {user_name}
┃
┠ <b>Rclone Config</b> → <b>{rccmsg}</b>
┠ <b>Rclone Flags</b> → <code>{rcflags}</code>
┖ <b>Rclone Path</b> → <code>{rccpath}</code>"""

    elif stype == "gdrive":
        buttons.data_button("token.pickle", f"userset {user_id} menu TOKEN_PICKLE")
        buttons.data_button("Default Gdrive ID", f"userset {user_id} menu GDRIVE_ID")
        buttons.data_button("Index URL", f"userset {user_id} menu INDEX_URL")
        if (
            user_dict.get("STOP_DUPLICATE", False)
            or "STOP_DUPLICATE" not in user_dict
            and Config.STOP_DUPLICATE
        ):
            buttons.data_button(
                "Disable Stop Duplicate", f"userset {user_id} tog STOP_DUPLICATE f"
            )
            sd_msg = "Enabled"
        else:
            buttons.data_button(
                "Enable Stop Duplicate",
                f"userset {user_id} tog STOP_DUPLICATE t",
                "l_body",
            )
            sd_msg = "Disabled"
        buttons.data_button("Back", f"userset {user_id} back mirror", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")

        tokenmsg = "Exists" if await aiopath.exists(token_pickle) else "Not Exists"
        if user_dict.get("GDRIVE_ID", False):
            gdrive_id = user_dict["GDRIVE_ID"]
        elif GDID := Config.GDRIVE_ID:
            gdrive_id = GDID
        else:
            gdrive_id = "None"
        index = user_dict["INDEX_URL"] if user_dict.get("INDEX_URL", False) else "None"
        btns = buttons.build_menu(2)

        text = f"""⌬ <b>GDrive Tools Settings :</b>
┟ <b>Name</b> → {user_name}
┃
┠ <b>Gdrive Token</b> → <b>{tokenmsg}</b>
┠ <b>Gdrive ID</b> → <code>{gdrive_id}</code>
┠ <b>Index URL</b> → <code>{index}</code>
┖ <b>Stop Duplicate</b> → <b>{sd_msg}</b>"""
    elif stype == "mirror":
        buttons.data_button("RClone Tools", f"userset {user_id} rclone")
        rccmsg = "Exists" if await aiopath.exists(rclone_conf) else "Not Exists"
        if user_dict.get("RCLONE_PATH", False):
            rccpath = user_dict["RCLONE_PATH"]
        elif RP := Config.RCLONE_PATH:
            rccpath = RP
        else:
            rccpath = "None"

        buttons.data_button("GDrive Tools", f"userset {user_id} gdrive")
        tokenmsg = "Exists" if await aiopath.exists(token_pickle) else "Not Exists"
        if user_dict.get("GDRIVE_ID", False):
            gdrive_id = user_dict["GDRIVE_ID"]
        elif GI := Config.GDRIVE_ID:
            gdrive_id = GI
        else:
            gdrive_id = "None"

        index = user_dict["INDEX_URL"] if user_dict.get("INDEX_URL", False) else "None"
        if (
            user_dict.get("STOP_DUPLICATE", False)
            or "STOP_DUPLICATE" not in user_dict
            and Config.STOP_DUPLICATE
        ):
            sd_msg = "Enabled"
        else:
            sd_msg = "Disabled"

        buttons.data_button("YT Up Tools", f"userset {user_id} yttools")
        buttons.data_button("Uphoster Tools", f"userset {user_id} uphoster")
        buttons.data_button("Back", f"userset {user_id} back", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        btns = buttons.build_menu(1)

        text = f"""⌬ <b>Mirror Settings :</b>
┟ <b>Name</b> → {user_name}
┃
┠ <b>Rclone Config</b> → <b>{rccmsg}</b>
┠ <b>Rclone Path</b> → <code>{rccpath}</code>
┠ <b>Gdrive Token</b> → <b>{tokenmsg}</b>
┠ <b>Gdrive ID</b> → <code>{gdrive_id}</code>
┠ <b>Index Link</b> → <code>{index}</code>
┖ <b>Stop Duplicate</b> → <b>{sd_msg}</b>
"""

    elif stype == "ffset":
        if user_dict.get("FFMPEG_CMDS", False):
            ffc = user_dict["FFMPEG_CMDS"]
        elif "FFMPEG_CMDS" not in user_dict and Config.FFMPEG_CMDS:
            ffc = Config.FFMPEG_CMDS
        else:
            ffc = "<b>Not Exists</b>"

        if isinstance(ffc, dict):
            ffc = "\n" + "\n".join(
                [
                    f"{no}. <b>{key}</b>: <code>{escape(str(value[0]))}</code>"
                    for no, (key, value) in enumerate(ffc.items(), start=1)
                ]
            )

        buttons.data_button(
            "Set Channel Metadata", f"userset {user_id} metachannel", "header"
        )
        buttons.data_button("Metadata", f"userset {user_id} menu METADATA")
        metadata_setting = user_dict.get("METADATA")
        display_meta_val = "<b>Not Set</b>"
        if isinstance(metadata_setting, dict) and metadata_setting:
            display_meta_val = ", ".join(
                f"{k}={escape(str(v))}" for k, v in metadata_setting.items()
            )
            display_meta_val = f"<code>{display_meta_val}</code>"
        elif isinstance(metadata_setting, str) and metadata_setting:  # Legacy
            display_meta_val = (
                f"<code>{escape(metadata_setting)}</code> [<i>Legacy, needs re-set</i>]"
            )

        buttons.data_button("Audio Metadata", f"userset {user_id} menu AUDIO_METADATA")
        audio_meta_setting = user_dict.get("AUDIO_METADATA")
        display_audio_meta = "<b>Not Set</b>"
        if isinstance(audio_meta_setting, dict) and audio_meta_setting:
            display_audio_meta = ", ".join(
                f"{k}={escape(str(v))}" for k, v in audio_meta_setting.items()
            )
            display_audio_meta = f"<code>{display_audio_meta}</code>"

        buttons.data_button("Video Metadata", f"userset {user_id} menu VIDEO_METADATA")
        video_meta_setting = user_dict.get("VIDEO_METADATA")
        display_video_meta = "<b>Not Set</b>"
        if isinstance(video_meta_setting, dict) and video_meta_setting:
            display_video_meta = ", ".join(
                f"{k}={escape(str(v))}" for k, v in video_meta_setting.items()
            )
            display_video_meta = f"<code>{display_video_meta}</code>"

        buttons.data_button(
            "Subtitle Metadata", f"userset {user_id} menu SUBTITLE_METADATA"
        )
        subtitle_meta_setting = user_dict.get("SUBTITLE_METADATA")
        display_subtitle_meta = "<b>Not Set</b>"
        if isinstance(subtitle_meta_setting, dict) and subtitle_meta_setting:
            display_subtitle_meta = ", ".join(
                f"{k}={escape(str(v))}" for k, v in subtitle_meta_setting.items()
            )
            display_subtitle_meta = f"<code>{display_subtitle_meta}</code>"

        buttons.data_button("Back", f"userset {user_id} back", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        btns = buttons.build_menu(2)

        text = f"""⌬ <b>FF Settings :</b>
┟ <b>Name</b> → {user_name}
┃
┠ <b>FF Media CLI</b> → hidden
┃
┠ <b>Default Metadata</b> → {display_meta_val}
┠ <b>Audio Metadata</b> → {display_audio_meta}
┠ <b>Video Metadata</b> → {display_video_meta}
┖ <b>Subtitle Metadata</b> → {display_subtitle_meta}"""

        text = f"""<b>Metadata Settings</b>
<b>Name:</b> {user_name}

Use <b>Set Channel Metadata</b> to clear old metadata and apply one channel name to global, video, audio, and subtitle metadata.

Default Metadata: {display_meta_val}
Audio Metadata: {display_audio_meta}
Video Metadata: {display_video_meta}
Subtitle Metadata: {display_subtitle_meta}"""

    elif stype == "advanced":
        buttons.data_button(
            "Excluded Extensions", f"userset {user_id} menu EXCLUDED_EXTENSIONS"
        )
        if user_dict.get("EXCLUDED_EXTENSIONS", False):
            ex_ex = user_dict["EXCLUDED_EXTENSIONS"]
        elif "EXCLUDED_EXTENSIONS" not in user_dict:
            ex_ex = excluded_extensions
        else:
            ex_ex = "None"

        if ex_ex != "None":
            ex_ex = ", ".join(ex_ex)

        ns_msg = (
            f"<code>{swap}</code>"
            if (swap := user_dict.get("NAME_SWAP", False))
            else "<b>Not Exists</b>"
        )
        buttons.data_button("Name Swap", f"userset {user_id} menu NAME_SWAP")

        buttons.data_button("YT-DLP Options", f"userset {user_id} menu YT_DLP_OPTIONS")
        if user_dict.get("YT_DLP_OPTIONS", False):
            ytopt = user_dict["YT_DLP_OPTIONS"]
        elif "YT_DLP_OPTIONS" not in user_dict and Config.YT_DLP_OPTIONS:
            ytopt = Config.YT_DLP_OPTIONS
        else:
            ytopt = "None"

        upload_paths = user_dict.get("UPLOAD_PATHS", {})
        if not upload_paths and "UPLOAD_PATHS" not in user_dict and Config.UPLOAD_PATHS:
            upload_paths = Config.UPLOAD_PATHS
        else:
            upload_paths = "None"
        buttons.data_button("Upload Paths", f"userset {user_id} menu UPLOAD_PATHS")

        yt_cookie_path = f"cookies/{user_id}/cookies.txt"
        user_cookie_msg = (
            "Exists" if await aiopath.exists(yt_cookie_path) else "Not Exists"
        )
        buttons.data_button(
            "YT Cookie File", f"userset {user_id} menu USER_COOKIE_FILE"
        )

        buttons.data_button("Back", f"userset {user_id} back", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        btns = buttons.build_menu(1)

        text = f"""⌬ <b>Advanced Settings :</b>
┟ <b>Name</b> → {user_name}
┃
┠ <b>Name Swaps</b> → {ns_msg}
┠ <b>Excluded Extensions</b> → <code>{ex_ex}</code>
┠ <b>Upload Paths</b> → <b>{upload_paths}</b>
┠ <b>YT-DLP Options</b> → <code>{ytopt}</code>
┖ <b>YT User Cookie File</b> → <b>{user_cookie_msg}</b>"""
    elif stype == "yttools":
        buttons.data_button("YT Description", f"userset {user_id} menu YT_DESP")
        yt_desp_val = user_dict.get(
            "YT_DESP",
            Config.YT_DESP if hasattr(Config, "YT_DESP") else "Not Set (Uses Default)",
        )

        buttons.data_button("YT Tags", f"userset {user_id} menu YT_TAGS")
        yt_tags_val = user_dict.get(
            "YT_TAGS",
            Config.YT_TAGS if hasattr(Config, "YT_TAGS") else "Not Set (Uses Default)",
        )
        if isinstance(yt_tags_val, list):
            yt_tags_val = ",".join(yt_tags_val)

        buttons.data_button("YT Category ID", f"userset {user_id} menu YT_CATEGORY_ID")
        yt_cat_id_val = user_dict.get(
            "YT_CATEGORY_ID",
            (
                Config.YT_CATEGORY_ID
                if hasattr(Config, "YT_CATEGORY_ID")
                else "Not Set (Uses Default)"
            ),
        )

        buttons.data_button(
            "YT Privacy Status", f"userset {user_id} menu YT_PRIVACY_STATUS"
        )
        yt_privacy_val = user_dict.get(
            "YT_PRIVACY_STATUS",
            (
                Config.YT_PRIVACY_STATUS
                if hasattr(Config, "YT_PRIVACY_STATUS")
                else "Not Set (Uses Default)"
            ),
        )

        buttons.data_button("Back", f"userset {user_id} back mirror", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        btns = buttons.build_menu(2)

        text = f"""⌬ <b>YouTube Tools Settings:</b>
┟ <b>Name</b> → {user_name}
┃
┠ <b>YT Description</b> → <code>{escape(str(yt_desp_val))}</code>
┠ <b>YT Tags</b> → <code>{escape(str(yt_tags_val))}</code>
┠ <b>YT Category ID</b> → <code>{escape(str(yt_cat_id_val))}</code>
┖ <b>YT Privacy Status</b> → <code>{escape(str(yt_privacy_val))}</code>"""

    return text, btns


async def update_user_settings(query, stype="main"):
    handler_dict[query.from_user.id] = False
    msg, button = await get_user_settings(query.from_user, stype)
    await edit_message(query.message, msg, button)


@new_task
async def send_user_settings(_, message):
    from_user = message.from_user
    handler_dict[from_user.id] = False
    msg, button = await get_user_settings(from_user)
    await send_message(message, msg, button)


@new_task
async def add_file(_, message, ftype, rfunc):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    if ftype == "THUMBNAIL":
        des_dir = await create_thumb(message, user_id)
    elif ftype == "RCLONE_CONFIG":
        rpath = f"{getcwd()}/rclone/"
        await makedirs(rpath, exist_ok=True)
        des_dir = f"{rpath}{user_id}.conf"
        await message.download(file_name=des_dir)
    elif ftype == "TOKEN_PICKLE":
        tpath = f"{getcwd()}/tokens/"
        await makedirs(tpath, exist_ok=True)
        des_dir = f"{tpath}{user_id}.pickle"
        await message.download(file_name=des_dir)
    elif ftype == "USER_COOKIE_FILE":
        cpath = f"{getcwd()}/cookies/{user_id}"
        await makedirs(cpath, exist_ok=True)
        des_dir = f"{cpath}/cookies.txt"
        await message.download(file_name=des_dir)
    elif ftype == "POST_LOGO":
        ppath = f"{getcwd()}/posters/{user_id}"
        await makedirs(ppath, exist_ok=True)
        des_dir = f"{ppath}/logo.png"
        await message.download(file_name=des_dir)
    await delete_message(message)
    update_user_ldata(user_id, ftype, des_dir)
    await rfunc()
    await database.update_user_doc(user_id, ftype, des_dir)


@new_task
async def add_one(_, message, option, rfunc):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    user_dict = user_data.get(user_id, {})
    value = message.text
    if value.startswith("{") and value.endswith("}"):
        try:
            value = eval(value)
            if user_dict[option]:
                user_dict[option].update(value)
            else:
                update_user_ldata(user_id, option, value)
        except Exception as e:
            await send_message(message, str(e))
            return
    else:
        await send_message(message, "It must be Dict!")
        return
    await delete_message(message)
    await rfunc()
    await database.update_user_data(user_id)


@new_task
async def remove_one(_, message, option, rfunc):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    user_dict = user_data.get(user_id, {})
    names = message.text.split("/")
    for name in names:
        if name in user_dict[option]:
            del user_dict[option][name]
    await delete_message(message)
    await rfunc()
    await database.update_user_data(user_id)


@new_task
async def set_option(_, message, option, rfunc):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    value = message.text
    auto_remove_enabled = user_data.get(user_id, {}).get(
        "AUTO_REMOVE_STREAMS", Config.AUTO_REMOVE_STREAMS
    )
    if (
        option
        in {
            "AUTO_KEEP_AUDIO_LANGS",
            "AUTO_KEEP_SUBTITLE_LANGS",
            "AUTO_AUDIO_ORDER",
            "AUTO_SUBTITLE_ORDER",
        }
        and str(value or "").strip()
        and auto_remove_enabled
    ):
        await send_message(
            message,
            "Auto Remove Streams is enabled. Disable it before setting Keep/Order values.",
        )
        return
    if option == "LEECH_SPLIT_SIZE":
        if not value.isdigit():
            value = get_size_bytes(value)
        value = min(int(value), TgClient.MAX_SPLIT_SIZE)
    elif option == "LEECH_FONT":
        value = "" if value.strip().lower() in {"none", "normal", "off"} else value.strip().lower()
        if value not in {"", "b", "i", "code"}:
            await send_message(message, "Caption font must be b, i, code, or none.")
            return
    # elif option == "LEECH_DUMP_CHAT": # TODO: Add
    elif option == "EXCLUDED_EXTENSIONS":
        fx = value.split()
        value = ["aria2", "!qB"]
        for x in fx:
            x = x.lstrip(".")
            value.append(x.strip().lower())
    elif option == "YT_TAGS":
        if isinstance(value, str):
            value = [tag.strip() for tag in value.split(",") if tag.strip()]
        elif not isinstance(value, list):
            await send_message(message, "YT Tags must be a comma-separated string.")
            return
    elif option == "YT_CATEGORY_ID":
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        elif not isinstance(value, int):
            await send_message(message, "YT Category ID must be a whole number.")
            return
    elif option == "YT_PRIVACY_STATUS":
        allowed_statuses = ["public", "private", "unlisted"]
        if not isinstance(value, str) or value.lower() not in allowed_statuses:
            await send_message(
                message,
                f"YT Privacy Status must be one of: {', '.join(allowed_statuses)}.",
            )
            return
        value = value.lower()
    elif option in [
        "METADATA",
        "AUDIO_METADATA",
        "VIDEO_METADATA",
        "SUBTITLE_METADATA",
    ]:
        parsed_metadata_dict = {}
        if value and isinstance(value, str):
            if value.strip() == "":
                value = {}
            else:
                parts = []
                current = ""
                i = 0
                while i < len(value):
                    if value[i] == "\\" and i + 1 < len(value) and value[i + 1] == "|":
                        current += "|"
                        i += 2
                    elif value[i] == "|":
                        parts.append(current)
                        current = ""
                        i += 1
                    else:
                        current += value[i]
                        i += 1
                if current:
                    parts.append(current)

                for part in parts:
                    if "=" in part:
                        key, val_str = part.split("=", 1)
                        parsed_metadata_dict[key.strip()] = val_str.strip()
                if not parsed_metadata_dict and value.strip() != "":
                    await send_message(
                        message,
                        "Malformed metadata string. Format: key1=value1|key2=value2. Use \\| to escape pipe characters.",
                    )
                    return
                value = parsed_metadata_dict
        else:
            value = {}

    elif option in ["UPLOAD_PATHS", "FFMPEG_CMDS", "YT_DLP_OPTIONS"]:
        if value.startswith("{") and value.endswith("}"):
            try:
                value = eval(sub(r"\s+", " ", value))
            except Exception as e:
                await send_message(message, str(e))
                return
        else:
            await send_message(message, "It must be dict!")
            return
    update_user_ldata(user_id, option, value)
    await delete_message(message)
    await rfunc()
    await database.update_user_data(user_id)


@new_task
async def set_channel_metadata(_, message, rfunc):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    channel = str(message.text or "").strip()
    if not channel:
        await send_message(message, "Send a channel name, for example: <code>@Anime_Starfall</code>")
        return

    clean_channel = " ".join(channel.split())
    if clean_channel.startswith("https://t.me/"):
        clean_channel = "@" + clean_channel.rsplit("/", 1)[-1].strip()
    brand_display = clean_channel
    brand = brand_display.replace("{", "{{").replace("}", "}}")
    safe_brand = (brand_display.lstrip("@") or brand_display).replace("{", "{{").replace("}", "}}")
    global_metadata = {
        "title": brand,
        "artist": brand,
        "album": brand,
        "album_artist": brand,
        "composer": brand,
        "genre": "Anime",
        "publisher": brand,
        "copyright": brand,
        "comment": f"Encoded by {brand}",
        "encoder": "FFmpeg",
        "description": brand,
        "synopsis": brand,
        "network": safe_brand,
    }
    video_metadata = {
        "title": brand,
        "handler_name": brand,
        "comment": brand,
        "encoder": brand,
    }
    audio_metadata = {
        "title": f"{brand} - {{audiolang}}",
        "handler_name": brand,
        "comment": brand,
        "encoder": brand,
    }
    subtitle_metadata = {
        "title": f"{brand} - {{sublang}}",
        "handler_name": brand,
        "comment": brand,
        "encoder": brand,
    }
    update_user_ldata(user_id, "METADATA", global_metadata)
    update_user_ldata(user_id, "VIDEO_METADATA", video_metadata)
    update_user_ldata(user_id, "AUDIO_METADATA", audio_metadata)
    update_user_ldata(user_id, "SUBTITLE_METADATA", subtitle_metadata)
    await delete_message(message)
    await send_message(
        message,
        f"Metadata channel set to <code>{escape(brand_display)}</code> for global, video, audio, and subtitles.",
    )
    await rfunc()
    await database.update_user_data(user_id)


def _reverse_autorename_template(sample):
    stem = str(sample or "").strip()
    if "." in stem:
        stem = ".".join(stem.split(".")[:-1]) or stem
    normalized = sub(r"[._]+", " ", stem)
    normalized = sub(r"\s+", " ", normalized).strip()
    has_episode = bool(search(r"(?i)S\d{1,2}\s*E\d{1,4}", normalized))
    has_year = bool(search(r"\b(19\d{2}|20\d{2}|21\d{2})\b", normalized))
    has_resolution = bool(search(r"(?i)\b(2160p|1080p|720p|480p)\b", normalized))
    has_bit = bool(search(r"(?i)\b(10|12)\s*bit\b", normalized))
    has_ott = bool(search(r"(?i)\b(NF|AMZN|DSNP|JHS|IMAX|HBO|CR)\b", normalized))
    has_quality = bool(search(r"(?i)\b(WEB[- ]?DL|WEB[- ]?Rip|BluRay|BDRip|BRRip|HDRip)\b", normalized))
    has_ds4k = bool(search(r"(?i)\bDS4K\b", normalized))
    has_codec = bool(search(r"(?i)\b(x265|x264|h265|h264|hevc|av1)\b", normalized))
    has_audio = bool(search(r"(?i)(DDP|EAC3|AC3|AAC|DTS|TrueHD|Opus|FLAC)(?:\s|-)*(2\.0|5\.1|7\.1|2 0|5 1|7 1)?", normalized))
    has_sub = bool(search(r"(?i)\b([EMS]?Sub|ESub|MSub)\b", stem))
    has_group = bool(search(r"\s~\s*[\w.-]+$", stem))

    parts = []
    if has_episode:
        parts.append("[S{season}E{episode}]")
    parts.append("{name}")
    if has_year:
        parts.append("{year}")
    if has_resolution:
        parts.append("{resolution}")
    if has_bit:
        parts.append("{bit}")
    if has_ott:
        parts.append("{ott}")
    if has_quality:
        parts.append("{quality}")
    if has_ds4k:
        parts.append("{DS4K}")
    if has_codec:
        parts.append("{codec}")
    if has_audio:
        parts.append("[{languages}-{audio_codec} {audio_channels}]")
    if has_sub:
        parts.append("{shortsub}")
    template = " ".join(parts)
    if has_group:
        template += " ~ {release_group}"
    return template


@new_task
async def create_autorename_template(_, message, rfunc):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    template = _reverse_autorename_template(message.text)
    update_user_ldata(user_id, "lremname_auto", template)
    await delete_message(message)
    await send_message(
        message,
        "<b>Reverse AutoRename template saved</b>\n\n"
        f"<code>{escape(template)}</code>",
    )
    await rfunc()
    await database.update_user_data(user_id)


async def _send_helper_error(message, text):
    await send_message(message, f"Helper token error: <code>{escape(str(text))}</code>")


@new_task
async def set_helper_pin(_, message, rfunc):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    try:
        await starfallx_upload.set_pin(user_id, message.text)
        await delete_message(message)
        await send_message(message, "Helper Token PIN saved. Settings are unlocked for 15 minutes.")
    except Exception as e:
        await _send_helper_error(message, e)
    await rfunc()


@new_task
async def unlock_helper_pin(_, message, rfunc):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    if starfallx_upload.verify_pin(user_id, message.text):
        await delete_message(message)
        await send_message(message, "Helper Token Settings unlocked for 15 minutes.")
    else:
        await delete_message(message)
        await send_message(message, "Wrong Helper Token PIN.")
    await rfunc()


@new_task
async def add_helper_token(_, message, rfunc):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    try:
        info = await starfallx_upload.set_user_token(user_id, message.text)
        await delete_message(message)
        await send_message(
            message,
            f"Helper token saved: @{escape(str(info.get('username')))}\n"
            "Add/keep this helper bot inside LEECH_DUMP_CHAT.",
        )
    except Exception as e:
        await delete_message(message)
        await _send_helper_error(message, e)
    await rfunc()


@new_task
async def remove_helper_token(_, message, rfunc):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    try:
        removed = await starfallx_upload.remove_user_token(user_id, message.text)
        await delete_message(message)
        await send_message(message, f"Removed helper tokens: <b>{removed}</b>")
    except Exception as e:
        await delete_message(message)
        await _send_helper_error(message, e)
    await rfunc()


@new_task
async def set_primary_helper_token(_, message, rfunc):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    try:
        record = await starfallx_upload.set_primary_token(user_id, message.text)
        await delete_message(message)
        await send_message(
            message,
            f"Primary helper set: @{escape(str(record.get('username') or 'Not Tested'))}",
        )
    except Exception as e:
        await delete_message(message)
        await _send_helper_error(message, e)
    await rfunc()


async def get_menu(option, message, user_id):
    handler_dict[user_id] = False
    user_dict = user_data.get(user_id, {})

    file_dict = {
        "THUMBNAIL": f"thumbnails/{user_id}.jpg",
        "RCLONE_CONFIG": f"rclone/{user_id}.conf",
        "TOKEN_PICKLE": f"tokens/{user_id}.pickle",
        "USER_COOKIE_FILE": f"cookies/{user_id}/cookies.txt",
        "POST_LOGO": f"posters/{user_id}/logo.png",
    }

    buttons = ButtonMaker()
    if option in ["THUMBNAIL", "RCLONE_CONFIG", "TOKEN_PICKLE", "USER_COOKIE_FILE", "POST_LOGO"]:
        key = "file"
    else:
        key = "set"
    buttons.data_button(
        "Set" if option == "lremname_auto" else ("Change" if user_dict.get(option, False) else "Set"),
        f"userset {user_id} {key} {option}",
    )
    if option == "lremname_auto":
        buttons.data_button("Create", f"userset {user_id} arcreate {option}", "header")
    if user_dict.get(option, False):
        if option == "THUMBNAIL":
            buttons.data_button(
                "View Thumb", f"userset {user_id} view THUMBNAIL", "header"
            )
        elif option in ["YT_DLP_OPTIONS", "FFMPEG_CMDS", "UPLOAD_PATHS"]:
            buttons.data_button(
                "Add One", f"userset {user_id} addone {option}", "header"
            )
            buttons.data_button(
                "Remove One", f"userset {user_id} rmone {option}", "header"
            )

        if key != "file":  # TODO: option default val check
            buttons.data_button("Reset", f"userset {user_id} reset {option}")
        elif await aiopath.exists(file_dict[option]):
            buttons.data_button("Remove", f"userset {user_id} remove {option}")
    if option in leech_options:
        back_to = "leech"
    elif option in auto_process_options:
        back_to = "autoprocess"
    elif option in rclone_options:
        back_to = "rclone"
    elif option in gdrive_options:
        back_to = "gdrive"
    elif option in yt_options:
        back_to = "yttools"
    elif option in post_options:
        back_to = "post"
    elif option in ffset_options:
        back_to = "ffset"
    elif option in advanced_options:
        back_to = "advanced"
    else:
        back_to = "back"
    buttons.data_button("Back", f"userset {user_id} {back_to}", "footer")
    buttons.data_button("Close", f"userset {user_id} close", "footer")
    val = user_dict.get(option)
    if option in file_dict and await aiopath.exists(file_dict[option]):
        val = "<b>Exists</b>"
    elif option == "LEECH_SPLIT_SIZE":
        val = get_readable_file_size(val)
    elif option == "METADATA":
        current_meta_val = user_dict.get(option)
        if isinstance(current_meta_val, dict) and current_meta_val:
            val = ", ".join(
                f"{k}={escape(str(v))}" for k, v in current_meta_val.items()
            )
            val = f"<code>{val}</code>"
        elif isinstance(current_meta_val, str) and current_meta_val:
            val = (
                f"<code>{escape(current_meta_val)}</code> [<i>Legacy, needs re-set</i>]"
            )
        elif not current_meta_val:
            val = "<b>Not Set</b>"

        if val is None:
            val = "<b>Not Exists</b>"

    if option == "METADATA":
        text = f"""⌬ <b><u>Menu Settings :</u></b>
│
┟ <b>Option</b> → {option}
┃
┠ <b>Option's Value</b> → {val if val else "<b>Not Exists</b>"}
┃
┠ <b>Default Input Type</b> → {user_settings_text[option][0]}
┠ <b>Description</b> → {user_settings_text[option][1]}
┃
┠ <b>Dynamic Variables:</b>
┠ • <code>{{filename}}</code> - Full filename
┠ • <code>{{basename}}</code> - Filename without extension  
┠ • <code>{{extension}}</code> - File extension
┃
┠ • <code>{{audiolang}}</code> - Audio language
┖ • <code>{{sublang}}</code> - Subtitle language
"""
    else:
        text = f"""⌬ <b><u>Menu Settings :</u></b>
│
┟ <b>Option</b> → {option}
┃
┠ <b>Option's Value</b> → {val if val else "<b>Not Exists</b>"}
┃
┠ <b>Default Input Type</b> → {user_settings_text[option][0]}
┖ <b>Description</b> → {user_settings_text[option][1]}
"""
    await edit_message(message, text, buttons.build_menu(2))


async def event_handler(client, query, pfunc, rfunc, photo=False, document=False):
    user_id = query.from_user.id
    handler_dict[user_id] = True
    start_time = update_time = time()

    async def event_filter(_, __, event):
        if photo:
            mtype = event.photo or event.document
        elif document:
            mtype = event.document
        else:
            mtype = event.text
        user = event.from_user or event.sender_chat
        return bool(
            user.id == user_id and event.chat.id == query.message.chat.id and mtype
        )

    handler = client.add_handler(
        MessageHandler(pfunc, filters=create(event_filter)), group=-1
    )

    while handler_dict[user_id]:
        await sleep(0.5)
        if time() - start_time > 60:
            handler_dict[user_id] = False
            await rfunc()
        elif time() - update_time > 8 and handler_dict[user_id]:
            update_time = time()
            msg = await client.get_messages(query.message.chat.id, query.message.id)
            text = msg.text.split("\n")
            text[-1] = (
                f"┖ <b>Time Left :</b> <code>{round(60 - (time() - start_time), 2)} sec</code>"
            )
            await edit_message(msg, "\n".join(text), msg.reply_markup)
    client.remove_handler(*handler)


async def send_user_settings_zip(message, user_id, user_dict):
    safe_settings = dict(user_dict)
    safe_settings.pop(HELPER_PIN_HASH_KEY, None)
    if HELPER_TOKENS_KEY in safe_settings:
        safe_settings[HELPER_TOKENS_KEY] = safe_helper_tokens(user_dict)
    if USER_BOT_TOKEN_KEY in safe_settings:
        safe_settings[USER_BOT_TOKEN_KEY] = safe_user_token_data(user_dict).get("token")
    payload = {
        "user_id": user_id,
        "settings": safe_settings,
    }
    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as zf:
        zf.writestr(
            "user_settings.json",
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        )
    archive.seek(0)
    archive.name = f"user_settings_{user_id}.zip"
    await send_file(message, archive, "User settings backup")


def _safe_import_user_settings(settings):
    deny_keys = {
        HELPER_TOKENS_KEY,
        HELPER_PIN_HASH_KEY,
        USER_BOT_TOKEN_KEY,
        "USER_UPLOAD_BOT_META",
        "USER_SESSION_STRING",
        "BOT_TOKEN",
        "DATABASE_URL",
    }
    safe = {}
    skipped = []
    for key, value in (settings or {}).items():
        key_u = str(key).upper()
        if key in deny_keys:
            skipped.append(key)
            continue
        if any(secret in key_u for secret in ("PASSWORD", "COOKIE", "SECRET")):
            skipped.append(key)
            continue
        if any(secret in key_u for secret in ("TOKEN", "SESSION")) and not isinstance(value, bool):
            skipped.append(key)
            continue
        if key_u.endswith("_KEY") and not isinstance(value, bool):
            skipped.append(key)
            continue
        if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
            safe[key] = value
    return safe, skipped


@new_task
async def import_user_settings_zip(_, message, rfunc):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    doc = message.document
    if not doc or not str(doc.file_name or "").lower().endswith(".zip"):
        await send_message(message, "Send a valid user settings .zip backup.")
        await rfunc()
        return
    file_path = await message.download()
    try:
        with ZipFile(file_path) as zf:
            with zf.open("user_settings.json") as fp:
                payload = json.loads(fp.read().decode("utf-8"))
        settings, skipped = _safe_import_user_settings(payload.get("settings", {}))
        if not settings:
            await send_message(
                message,
                f"No safe user settings found to import. Protected/skipped keys: {len(skipped)}.",
            )
        else:
            user_data.setdefault(user_id, {}).update(settings)
            await database.update_user_data(user_id)
            await send_message(
                message,
                f"Imported {len(settings)} safe user settings. Protected/skipped keys: {len(skipped)}.",
            )
    except Exception as e:
        await send_message(message, f"Settings import failed: <code>{escape(str(e))}</code>")
    finally:
        with suppress(Exception):
            await remove(file_path)
    await delete_message(message)
    await rfunc()


@new_task
async def edit_user_settings(client, query):
    from_user = query.from_user
    user_id = from_user.id
    name = from_user.mention
    message = query.message
    data = query.data.split()

    handler_dict[user_id] = False
    thumb_path = f"thumbnails/{user_id}.jpg"
    rclone_conf = f"rclone/{user_id}.conf"
    token_pickle = f"tokens/{user_id}.pickle"
    yt_cookie_path = f"cookies/{user_id}/cookies.txt"
    post_logo_path = f"posters/{user_id}/logo.png"

    user_dict = user_data.get(user_id, {})
    if user_id != int(data[1]):
        return await query.answer("Not Yours!", show_alert=True)
    elif data[2] == "setevent":
        await query.answer()
    elif data[2] == "zip":
        if (
            config_bool(Config.HELPER_TOKEN_PIN_REQUIRED, True)
            and user_dict.get(HELPER_TOKENS_KEY)
            and not starfallx_upload.pin_unlocked(user_id)
        ):
            await query.answer("Unlock Helper Token PIN before export.", show_alert=True)
            await update_user_settings(query, "userbot")
            return
        await query.answer("Preparing zip backup...")
        await send_user_settings_zip(message, user_id, user_dict)
    elif data[2] == "zipimport":
        await query.answer()
        buttons = ButtonMaker()
        buttons.data_button("Stop", f"userset {user_id} back")
        buttons.data_button("Back", f"userset {user_id} back", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        await edit_message(
            message,
            "Send your user settings backup .zip.\nRaw helper tokens, PINs, cookies, passwords, and API keys will not be imported.\nTimeout: 60 sec",
            buttons.build_menu(1),
        )
        rfunc = partial(update_user_settings, query, "main")
        pfunc = partial(import_user_settings_zip, rfunc=rfunc)
        await event_handler(client, query, pfunc, rfunc, document=True)
    elif data[2] == "font":
        await query.answer()
        current = user_dict.get("LEECH_FONT", Config.LEECH_FONT) or ""
        order = ["", "b", "i"]
        next_value = order[(order.index(current) + 1) % len(order)] if current in order else ""
        update_user_ldata(user_id, "LEECH_FONT", next_value)
        await database.update_user_data(user_id)
        await update_user_settings(query, "leech")
    elif data[2] in [
        "general",
        "mirror",
        "leech",
        "userbot",
        "userbot_tokens",
        "userbot_backups",
        "userbot_status",
        "autoprocess",
        "uphoster",
        "gofile",
        "buzzheavier",
        "pixeldrain",
        "ffset",
        "advanced",
        "post",
        "posttemplate",
        "gdrive",
        "rclone",
    ]:
        await query.answer()
        await update_user_settings(query, data[2])
    elif data[2] == "posttemplateset":
        await query.answer("Template saved.", show_alert=True)
        template_id = data[3] if len(data) > 3 and data[3] in {str(i) for i in range(1, 9)} else "1"
        update_user_ldata(user_id, "POST_TEMPLATE_ID", int(template_id))
        await database.update_user_data(user_id)
        await update_user_settings(query, "posttemplate")
    elif data[2] in [
        "helperpinset",
        "helperunlock",
        "helperadd",
        "helperremove",
        "helpersetprimary",
    ]:
        prompts = {
            "helperpinset": "Send a new Helper Token PIN.\nTimeout: 60 sec",
            "helperunlock": "Send your Helper Token PIN.\nTimeout: 60 sec",
            "helperadd": "Send one helper bot token.\nExample: 123456:ABCDEF\nTimeout: 60 sec",
            "helperremove": "Send token number, @username, bot id, or token id prefix to remove.\nTimeout: 60 sec",
            "helpersetprimary": "Send token number, @username, bot id, or token id prefix to make primary.\nTimeout: 60 sec",
        }
        funcs = {
            "helperpinset": set_helper_pin,
            "helperunlock": unlock_helper_pin,
            "helperadd": add_helper_token,
            "helperremove": remove_helper_token,
            "helpersetprimary": set_primary_helper_token,
        }
        if (
            data[2] not in ["helperpinset", "helperunlock"]
            and config_bool(Config.HELPER_TOKEN_PIN_REQUIRED, True)
            and not starfallx_upload.pin_unlocked(user_id)
        ):
            await query.answer("Unlock Helper Token PIN first.", show_alert=True)
            await update_user_settings(query, "userbot")
            return
        await query.answer()
        buttons = ButtonMaker()
        buttons.data_button("Stop", f"userset {user_id} userbot")
        buttons.data_button("Back", f"userset {user_id} userbot", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        await edit_message(message, prompts[data[2]], buttons.build_menu(1))
        rfunc = partial(update_user_settings, query, "userbot")
        pfunc = partial(funcs[data[2]], rfunc=rfunc)
        await event_handler(client, query, pfunc, rfunc)
    elif data[2] == "metachannel":
        await query.answer()
        buttons = ButtonMaker()
        buttons.data_button("Stop", f"userset {user_id} ffset")
        buttons.data_button("Back", f"userset {user_id} ffset", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        await edit_message(
            message,
            "Send the channel name to use for all metadata.\nExample: <code>@Anime_Starfall</code>\nTimeout: 60 sec",
            buttons.build_menu(1),
        )
        rfunc = partial(update_user_settings, query, "ffset")
        pfunc = partial(set_channel_metadata, rfunc=rfunc)
        await event_handler(client, query, pfunc, rfunc)
    elif data[2] == "helpertest":
        if config_bool(Config.HELPER_TOKEN_PIN_REQUIRED, True) and not starfallx_upload.pin_unlocked(user_id):
            await query.answer("Unlock Helper Token PIN first.", show_alert=True)
            await update_user_settings(query, "userbot")
            return
        await query.answer("Testing helper tokens...")
        results = await starfallx_upload.test_user_tokens(user_id)
        if not results:
            await send_message(message, "No helper tokens saved.")
        else:
            lines = ["<b>Helper Token Test</b>"]
            for index, (record, ok, status) in enumerate(results, start=1):
                username = record.get("username") or "Not Tested"
                mark = "OK" if ok else "FAIL"
                lines.append(f"{index}. @{escape(str(username))} -> {mark} ({status})")
            await send_message(message, "\n".join(lines))
        await update_user_settings(query, "userbot")
    elif data[2] == "helperlock":
        starfallx_upload.lock_pin(user_id)
        await query.answer("Helper Token Settings locked.", show_alert=True)
        await update_user_settings(query, "userbot")
    elif data[2] == "yttools":
        await query.answer()
        await update_user_settings(query, data[2])
    elif data[2] == "uphoster_destinations":
        await query.answer()
        user_dict = user_data.get(user_id, {})
        uphoster_service = user_dict.get("UPHOSTER_SERVICE", "gofile")
        selected_services = uphoster_service.split(",") if uphoster_service else []

        if len(data) > 3:
            service = data[3]
            if service in selected_services:
                if len(selected_services) > 1:
                    selected_services.remove(service)
                else:
                    await query.answer(
                        "At least one destination must be selected!", show_alert=True
                    )
            else:
                selected_services.append(service)
            new_services = ",".join(selected_services)
            update_user_ldata(user_id, "UPHOSTER_SERVICE", new_services)
            await database.update_user_data(user_id)
            selected_services = new_services.split(",")
        else:
            selected_services = (
                uphoster_service.split(",") if uphoster_service else ["gofile"]
            )

        buttons = ButtonMaker()
        for service in ["gofile", "buzzheavier", "pixeldrain"]:
            state = "✓" if service in selected_services else ""
            buttons.data_button(
                f"{service.capitalize()} {state}",
                f"userset {user_id} uphoster_destinations {service}",
            )

        buttons.data_button("Back", f"userset {user_id} back uphoster", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")

        text = f"""⌬ <b>Select Uphoster Destinations :</b>"""
        await edit_message(message, text, buttons.build_menu(1))
    elif data[2] == "menu":
        if data[3] in {
            "AUTO_KEEP_AUDIO_LANGS",
            "AUTO_KEEP_SUBTITLE_LANGS",
            "AUTO_AUDIO_ORDER",
            "AUTO_SUBTITLE_ORDER",
        } and user_data.get(user_id, {}).get("AUTO_REMOVE_STREAMS", Config.AUTO_REMOVE_STREAMS):
            await query.answer(
                "Disable Auto Remove Streams before setting Keep/Order values.",
                show_alert=True,
            )
            return
        await query.answer()
        await get_menu(data[3], message, user_id)
    elif data[2] == "tog":
        key = data[3]
        enabling = data[4] == "t"
        if key == "AUTO_ORDER" and enabling and user_data.get(user_id, {}).get("AUTO_REMOVE_STREAMS", Config.AUTO_REMOVE_STREAMS):
            await query.answer(
                "Disable Auto Remove Streams before enabling Auto Order.",
                show_alert=True,
            )
            return
        await query.answer()
        if data[3] == "RENAME_METHOD":
            update_user_ldata(user_id, data[3], data[4])
        else:
            update_user_ldata(user_id, data[3], data[4] == "t")
        if key == "AUTO_REMOVE_STREAMS" and enabling:
            for conflict_key in (
                "AUTO_KEEP_AUDIO_LANGS",
                "AUTO_KEEP_SUBTITLE_LANGS",
                "AUTO_AUDIO_ORDER",
                "AUTO_SUBTITLE_ORDER",
            ):
                update_user_ldata(user_id, conflict_key, "")
            update_user_ldata(user_id, "AUTO_ORDER", False)
        if data[3] == "STOP_DUPLICATE":
            back_to = "gdrive"
        elif data[3] in ["USER_TOKENS", "USE_DEFAULT_COOKIE"]:
            back_to = "general"
        elif data[3] in ["AUTO_POSTER_ENABLED", "AUTO_POSTER_USE_AS_THUMBNAIL"]:
            back_to = "post"
        elif data[3].startswith("AUTO_") and data[3] != "AUTO_THUMBNAIL":
            back_to = "autoprocess"
        else:
            back_to = "leech"
        await update_user_settings(query, stype=back_to)
        await database.update_user_data(user_id)
    elif data[2] == "file":
        await query.answer()
        buttons = ButtonMaker()
        text = user_settings_text[data[3]][2]
        buttons.data_button("Stop", f"userset {user_id} menu {data[3]} stop")
        buttons.data_button("Back", f"userset {user_id} menu {data[3]}", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        prompt_title = data[3].replace("_", " ").title()
        new_message_text = f"⌬ <b>Set {prompt_title}</b>\n\n{text}"
        await edit_message(message, new_message_text, buttons.build_menu(1))
        rfunc = partial(get_menu, data[3], message, user_id)
        pfunc = partial(add_file, ftype=data[3], rfunc=rfunc)
        await event_handler(
            client,
            query,
            pfunc,
            rfunc,
            photo=data[3] in ["THUMBNAIL", "POST_LOGO"],
            document=data[3] not in ["THUMBNAIL", "POST_LOGO"],
        )
    elif data[2] == "arcreate":
        await query.answer()
        buttons = ButtonMaker()
        text = (
            "Send one sample final filename.\n"
            "Example: <code>[S01E08] Off Campus (2026) 1080p 10bit AMZN WEBRip x265 [Tamil-DDP 5.1] ESub ~ PSA.mkv</code>\n"
            "<b>Time Left:</b> <code>60 sec</code>"
        )
        buttons.data_button("Stop", f"userset {user_id} menu {data[3]} stop")
        buttons.data_button("Back", f"userset {user_id} menu {data[3]}", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        await edit_message(message, message.text.html + "\n\n" + text, buttons.build_menu(1))
        rfunc = partial(get_menu, data[3], message, user_id)
        pfunc = partial(create_autorename_template, rfunc=rfunc)
        await event_handler(client, query, pfunc, rfunc)
    elif data[2] in ["set", "addone", "rmone"]:
        await query.answer()
        buttons = ButtonMaker()
        if data[2] == "set":
            text = user_settings_text[data[3]][2]
            func = set_option
        elif data[2] == "addone":
            text = f"Add one or more string key and value to {data[3]}. Example: {{'key 1': 62625261, 'key 2': 'value 2'}}. Timeout: 60 sec"
            func = add_one
        elif data[2] == "rmone":
            text = f"Remove one or more key from {data[3]}. Example: key 1/key2/key 3. Timeout: 60 sec"
            func = remove_one
        buttons.data_button("Stop", f"userset {user_id} menu {data[3]} stop")
        buttons.data_button("Back", f"userset {user_id} menu {data[3]}", "footer")
        buttons.data_button("Close", f"userset {user_id} close", "footer")
        await edit_message(
            message, message.text.html + "\n\n" + text, buttons.build_menu(1)
        )
        rfunc = partial(get_menu, data[3], message, user_id)
        pfunc = partial(func, option=data[3], rfunc=rfunc)
        await event_handler(client, query, pfunc, rfunc)
    elif data[2] == "remove":
        await query.answer("Removed!", show_alert=True)
        if data[3] in [
            "THUMBNAIL",
            "RCLONE_CONFIG",
            "TOKEN_PICKLE",
            "USER_COOKIE_FILE",
            "POST_LOGO",
        ]:
            if data[3] == "THUMBNAIL":
                fpath = thumb_path
            elif data[3] == "RCLONE_CONFIG":
                fpath = rclone_conf
            elif data[3] == "USER_COOKIE_FILE":
                fpath = yt_cookie_path
            elif data[3] == "POST_LOGO":
                fpath = post_logo_path
            else:
                fpath = token_pickle
            if await aiopath.exists(fpath):
                await remove(fpath)
            del user_dict[data[3]]
            await database.update_user_doc(user_id, data[3])
        else:
            update_user_ldata(user_id, data[3], "")
            await database.update_user_data(user_id)
        await get_menu(data[3], message, user_id)
    elif data[2] == "reset":
        await query.answer("Reset Done!", show_alert=True)
        user_dict.pop(data[3], None)
        await database.update_user_data(user_id)
        await get_menu(data[3], message, user_id)
    elif data[2] == "confirm_reset_all":
        await query.answer()
        buttons = ButtonMaker()
        buttons.data_button(f"{GREEN_DOT} Yes", f"userset {user_id} do_reset_all yes", style=ButtonStyle.SUCCESS)
        buttons.data_button(f"{RED_DOT} No", f"userset {user_id} do_reset_all no", style=ButtonStyle.DANGER)
        buttons.data_button(f"{RED_DOT} Close", f"userset {user_id} close", "footer", style=ButtonStyle.DANGER)
        text = "<i>Are you sure you want to reset all your user settings?</i>"
        await edit_message(query.message, text, buttons.build_menu(2))
    elif data[2] == "do_reset_all":
        if data[3] == "yes":
            await query.answer("Reset Done!", show_alert=True)
            user_dict = user_data.get(user_id, {})
            for k in list(user_dict.keys()):
                if k not in ("SUDO", "AUTH", "VERIFY_TOKEN", "VERIFY_TIME"):
                    del user_dict[k]
            for fpath in [thumb_path, rclone_conf, token_pickle, yt_cookie_path, post_logo_path]:
                if await aiopath.exists(fpath):
                    await remove(fpath)
            await update_user_settings(query)
            await database.update_user_data(user_id)
        else:
            await query.answer("Reset Cancelled.", show_alert=True)
            await update_user_settings(query)
    elif data[2] == "view":
        await query.answer()
        await send_file(message, thumb_path, name)
    elif data[2] in ["gd", "rc"]:
        await query.answer()
        du = "rc" if data[2] == "gd" else "gd"
        update_user_ldata(user_id, "DEFAULT_UPLOAD", du)
        await update_user_settings(query, stype="general")
        await database.update_user_data(user_id)
    elif data[2] == "back":
        await query.answer()
        stype = data[3] if len(data) == 4 else "main"
        await update_user_settings(query, stype)
    else:
        await query.answer()
        await delete_message(message, message.reply_to_message)


@new_task
async def get_users_settings(_, message):
    msg = ""
    if auth_chats:
        msg += f"AUTHORIZED_CHATS: {auth_chats}\n"
    if sudo_users:
        msg += f"SUDO_USERS: {sudo_users}\n\n"
    if user_data:
        for u, d in user_data.items():
            kmsg = f"\n<b>{u}:</b>\n"
            def _safe_display(key, value):
                if key == HELPER_PIN_HASH_KEY:
                    return "PIN Set"
                if key == HELPER_TOKENS_KEY:
                    return safe_helper_tokens(d)
                if key == USER_BOT_TOKEN_KEY:
                    return safe_user_token_data(d).get("token")
                return value

            if vmsg := "".join(
                f"{k}: <code>{_safe_display(k, v) or None}</code>\n" for k, v in d.items()
            ):
                msg += kmsg + vmsg
        if not msg:
            await send_message(message, "No users data!")
            return
        msg_ecd = msg.encode()
        if len(msg_ecd) > 4000:
            with BytesIO(msg_ecd) as ofile:
                ofile.name = "users_settings.txt"
                await send_file(message, ofile)
        else:
            await send_message(message, msg)
    else:
        await send_message(message, "No users data!")
