from asyncio import Event, wait_for
from ast import literal_eval
from functools import partial
from time import time

from httpx import AsyncClient
from aiofiles.os import path as aiopath
from yt_dlp import YoutubeDL
from pyrogram.filters import regex, user
from pyrogram.handlers import CallbackQueryHandler

from .. import DOWNLOAD_DIR, LOGGER, bot_loop, task_dict_lock
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import (
    COMMAND_USAGE,
    arg_parser,
    new_task,
    sync_to_async,
)
from ..helper.ext_utils.links_utils import is_url
from ..helper.ext_utils.media_utils import download_image_thumb
from ..helper.ext_utils.site_resolvers import resolve_external_site
from ..helper.ext_utils.task_manager import pre_task_check
from ..helper.ext_utils.status_utils import get_readable_file_size, get_readable_time
from ..helper.listeners.task_listener import TaskListener
from ..helper.mirror_leech_utils.download_utils.yt_dlp_download import YoutubeDLHelper
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    auto_delete_message,
    delete_links,
    delete_message,
    edit_message,
    send_message,
)

SITE_OPTION_KEYS = {
    "mx_api_base",
    "MX_PLAYER_API_BASE",
    "mx_audio",
    "mx_quality",
    "hanime_api_base",
    "HANIME_API_BASE",
    "hanime_quality",
}


@new_task
async def select_format(_, query, obj):
    data = query.data.split()
    message = query.message
    await query.answer()

    if data[1] == "dict":
        b_name = data[2]
        await obj.qual_subbuttons(b_name)
    elif data[1] == "mp3":
        await obj.mp3_subbuttons()
    elif data[1] == "audio":
        await obj.audio_format()
    elif data[1] == "aq":
        if data[2] == "back":
            await obj.audio_format()
        else:
            await obj.audio_quality(data[2])
    elif data[1] == "back":
        await obj.back_to_main()
    elif data[1] == "cancel":
        await edit_message(message, "Task has been cancelled.")
        obj.qual = None
        obj.listener.is_cancelled = True
        obj.event.set()
    elif data[1].startswith("site_"):
        await obj.site_callback(data[1:])
    else:
        if data[1] == "sub":
            obj.qual = obj.formats[data[2]][data[3]][1]
        elif "|" in data[1]:
            obj.qual = obj.formats[data[1]]
        else:
            obj.qual = data[1]
        obj.event.set()


class YtSelection:
    def __init__(self, listener):
        self.listener = listener
        self._is_m4a = False
        self._reply_to = None
        self._time = time()
        self._timeout = 120
        self._is_playlist = False
        self._main_buttons = None
        self.event = Event()
        self.formats = {}
        self.qual = None

    async def _event_handler(self):
        pfunc = partial(select_format, obj=self)
        handler = self.listener.client.add_handler(
            CallbackQueryHandler(
                pfunc, filters=regex("^ytq") & user(self.listener.user_id)
            ),
            group=-1,
        )
        try:
            await wait_for(self.event.wait(), timeout=self._timeout)
        except Exception:
            await edit_message(self._reply_to, "Timed Out. Task has been cancelled!")
            self.qual = None
            self.listener.is_cancelled = True
            self.event.set()
        finally:
            self.listener.client.remove_handler(*handler)

    async def get_quality(self, result):
        buttons = ButtonMaker()
        if "entries" in result:
            self._is_playlist = True
            for i in ["144", "240", "360", "480", "720", "1080", "1440", "2160"]:
                video_format = f"bv*[height<=?{i}][ext=mp4]+ba[ext=m4a]/b[height<=?{i}]"
                b_data = f"{i}|mp4"
                self.formats[b_data] = video_format
                buttons.data_button(f"{i}-mp4", f"ytq {b_data}")
                video_format = f"bv*[height<=?{i}][ext=webm]+ba/b[height<=?{i}]"
                b_data = f"{i}|webm"
                self.formats[b_data] = video_format
                buttons.data_button(f"{i}-webm", f"ytq {b_data}")
            buttons.data_button("MP3", "ytq mp3")
            buttons.data_button("Audio Formats", "ytq audio")
            buttons.data_button("Best Videos", "ytq bv*+ba/b")
            buttons.data_button("Best Audios", "ytq ba/b")
            buttons.data_button("Cancel", "ytq cancel", "footer")
            self._main_buttons = buttons.build_menu(3)
            msg = f"Choose Playlist Videos Quality:\nTimeout: {get_readable_time(self._timeout - (time() - self._time))}"
        else:
            format_dict = result.get("formats")
            if format_dict is not None:
                for item in format_dict:
                    if item.get("tbr"):
                        format_id = item["format_id"]

                        if item.get("filesize"):
                            size = item["filesize"]
                        elif item.get("filesize_approx"):
                            size = item["filesize_approx"]
                        else:
                            size = 0

                        if item.get("video_ext") == "none" and (
                            item.get("resolution") == "audio only"
                            or item.get("acodec") != "none"
                        ):
                            if item.get("audio_ext") == "m4a":
                                self._is_m4a = True
                            b_name = f"{item.get('acodec') or format_id}-{item['ext']}"
                            v_format = format_id
                        elif item.get("height"):
                            height = item["height"]
                            ext = item["ext"]
                            fps = item["fps"] if item.get("fps") else ""
                            b_name = f"{height}p{fps}-{ext}"
                            ba_ext = (
                                "[ext=m4a]" if self._is_m4a and ext == "mp4" else ""
                            )
                            v_format = f"{format_id}+ba{ba_ext}/b[height=?{height}]"
                        else:
                            continue

                        self.formats.setdefault(b_name, {})[f"{item['tbr']}"] = [
                            size,
                            v_format,
                        ]

                for b_name, tbr_dict in self.formats.items():
                    if len(tbr_dict) == 1:
                        tbr, v_list = next(iter(tbr_dict.items()))
                        buttonName = f"{b_name} ({get_readable_file_size(v_list[0])})"
                        buttons.data_button(buttonName, f"ytq sub {b_name} {tbr}")
                    else:
                        buttons.data_button(b_name, f"ytq dict {b_name}")
            buttons.data_button("MP3", "ytq mp3")
            buttons.data_button("Audio Formats", "ytq audio")
            buttons.data_button("Best Video", "ytq bv*+ba/b")
            buttons.data_button("Best Audio", "ytq ba/b")
            buttons.data_button("Cancel", "ytq cancel", "footer")
            self._main_buttons = buttons.build_menu(2)
            msg = f"Choose Video Quality:\nTimeout: {get_readable_time(self._timeout - (time() - self._time))}"
        self._reply_to = await send_message(
            self.listener.message, msg, self._main_buttons
        )
        await self._event_handler()
        if not self.listener.is_cancelled:
            await delete_message(self._reply_to)
        return self.qual

    async def back_to_main(self):
        if self._is_playlist:
            msg = f"Choose Playlist Videos Quality:\nTimeout: {get_readable_time(self._timeout - (time() - self._time))}"
        else:
            msg = f"Choose Video Quality:\nTimeout: {get_readable_time(self._timeout - (time() - self._time))}"
        await edit_message(self._reply_to, msg, self._main_buttons)

    async def qual_subbuttons(self, b_name):
        buttons = ButtonMaker()
        tbr_dict = self.formats[b_name]
        for tbr, d_data in tbr_dict.items():
            button_name = f"{tbr}K ({get_readable_file_size(d_data[0])})"
            buttons.data_button(button_name, f"ytq sub {b_name} {tbr}")
        buttons.data_button("Back", "ytq back", "footer")
        buttons.data_button("Cancel", "ytq cancel", "footer")
        subbuttons = buttons.build_menu(2)
        msg = f"Choose Bit rate for <b>{b_name}</b>:\nTimeout: {get_readable_time(self._timeout - (time() - self._time))}"
        await edit_message(self._reply_to, msg, subbuttons)

    async def mp3_subbuttons(self):
        i = "s" if self._is_playlist else ""
        buttons = ButtonMaker()
        audio_qualities = [64, 128, 320]
        for q in audio_qualities:
            audio_format = f"ba/b-mp3-{q}"
            buttons.data_button(f"{q}K-mp3", f"ytq {audio_format}")
        buttons.data_button("Back", "ytq back")
        buttons.data_button("Cancel", "ytq cancel")
        subbuttons = buttons.build_menu(3)
        msg = f"Choose mp3 Audio{i} Bitrate:\nTimeout: {get_readable_time(self._timeout - (time() - self._time))}"
        await edit_message(self._reply_to, msg, subbuttons)

    async def audio_format(self):
        i = "s" if self._is_playlist else ""
        buttons = ButtonMaker()
        for frmt in ["aac", "alac", "flac", "m4a", "opus", "vorbis", "wav"]:
            audio_format = f"ba/b-{frmt}-"
            buttons.data_button(frmt, f"ytq aq {audio_format}")
        buttons.data_button("Back", "ytq back", "footer")
        buttons.data_button("Cancel", "ytq cancel", "footer")
        subbuttons = buttons.build_menu(3)
        msg = f"Choose Audio{i} Format:\nTimeout: {get_readable_time(self._timeout - (time() - self._time))}"
        await edit_message(self._reply_to, msg, subbuttons)

    async def audio_quality(self, format):
        i = "s" if self._is_playlist else ""
        buttons = ButtonMaker()
        for qual in range(11):
            audio_format = f"{format}{qual}"
            buttons.data_button(str(qual), f"ytq {audio_format}")
        buttons.data_button("Back", "ytq aq back")
        buttons.data_button("Cancel", "ytq cancel")
        subbuttons = buttons.build_menu(5)
        msg = f"Choose Audio{i} Quality:\n0 is best and 10 is worst\nTimeout: {get_readable_time(self._timeout - (time() - self._time))}"
        await edit_message(self._reply_to, msg, subbuttons)


class SiteSelection:
    def __init__(self, listener, site_data, options):
        self.listener = listener
        self.site_data = site_data
        self.options = options or {}
        self._reply_to = None
        self._time = time()
        self._timeout = max(15, int(getattr(Config, "SITE_QUALITY_SELECTOR_TIMEOUT", 120) or 120))
        self.event = Event()
        self.qual = None
        self.selected_videos = set()
        self.selected_audio = set()

    async def _event_handler(self):
        pfunc = partial(select_format, obj=self)
        handler = self.listener.client.add_handler(
            CallbackQueryHandler(
                pfunc, filters=regex("^ytq") & user(self.listener.user_id)
            ),
            group=-1,
        )
        try:
            await wait_for(self.event.wait(), timeout=self._timeout)
        except Exception:
            await edit_message(self._reply_to, "Timed Out. Task has been cancelled!")
            self.qual = None
            self.listener.is_cancelled = True
            self.event.set()
        finally:
            self.listener.client.remove_handler(*handler)

    def _timeout_text(self):
        return get_readable_time(max(0, self._timeout - (time() - self._time)))

    def _default_list(self, key):
        value = self.options.get(key)
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip().lower() for item in value if str(item).strip()]
        return [part.strip().lower() for part in str(value).split(",") if part.strip()]

    async def get_quality(self):
        if self.site_data["type"] == "mx":
            result = self._preset_mx_result()
            if result:
                return result
            await self._show_mx_videos()
        else:
            result = self._preset_hanime_result()
            if result:
                return result
            await self._show_hanime_streams()
        await self._event_handler()
        if not self.listener.is_cancelled and self._reply_to:
            await delete_message(self._reply_to)
        return self.qual

    def _preset_mx_result(self):
        desired = self._default_list("mx_quality")
        if not desired:
            return None
        if any(item in {"all", "*"} for item in desired):
            videos = self.site_data.get("videos", [])
        else:
            videos = [
                item
                for item in self.site_data.get("videos", [])
                if str(item.get("height") or "").lower() in desired
                or str(item.get("label") or "").split("p", 1)[0].lower() in desired
            ]
        if not videos:
            return None
        audio_pref = str(
            self.options.get("mx_audio")
            or getattr(Config, "MX_DEFAULT_AUDIO", "ask")
            or "ask"
        ).strip().lower()
        audios = self.site_data.get("audios", [])
        if audio_pref in {"ask", ""}:
            return None
        selected_audio = []
        if audio_pref in {"all", "multi", "multiaudio"}:
            selected_audio = [item["id"] for item in audios]
        elif audio_pref not in {"none", "skip", "video"}:
            wanted = [part.strip().lower() for part in audio_pref.split(",") if part.strip()]
            selected_audio = [
                item["id"]
                for item in audios
                if str(item.get("language", "")).lower() in wanted
                or any(w in str(item.get("label", "")).lower() for w in wanted)
            ]
        return self._build_mx_result(videos, selected_audio)

    def _preset_hanime_result(self):
        desired = self._default_list("hanime_quality")
        if not desired:
            return None
        if any(item in {"all", "*"} for item in desired):
            streams = self.site_data.get("streams", [])
        else:
            streams = [
                item
                for item in self.site_data.get("streams", [])
                if str(item.get("height") or "").lower() in desired
                or str(item.get("label") or "").split("p", 1)[0].lower() in desired
            ]
        if not streams:
            return None
        return self._build_hanime_result(streams)

    async def _show_mx_videos(self):
        buttons = ButtonMaker()
        videos = self.site_data.get("videos", [])
        if not self.selected_videos and videos:
            self.selected_videos.add(0)
        for index, item in enumerate(videos[:25]):
            mark = "[x]" if index in self.selected_videos else "[ ]"
            buttons.data_button(f"{mark} {item['label']}", f"ytq site_mxv {index}")
        buttons.data_button("All Qualities", "ytq site_mxvall")
        buttons.data_button("Next", "ytq site_mxnext", "footer")
        buttons.data_button("Cancel", "ytq cancel", "footer")
        text = (
            f"Choose MX Player quality for:\n<b>{self.site_data['title']}</b>\n"
            f"Tap multiple qualities if needed.\nTimeout: {self._timeout_text()}"
        )
        menu = buttons.build_menu(2)
        if self._reply_to:
            await edit_message(self._reply_to, text, menu)
        else:
            self._reply_to = await send_message(self.listener.message, text, menu)

    async def _show_mx_audio(self):
        audios = self.site_data.get("audios", [])
        if not audios:
            self.qual = self._build_mx_result(
                [self.site_data["videos"][idx] for idx in sorted(self.selected_videos)],
                [],
            )
            self.event.set()
            return
        if not self.selected_audio and str(getattr(Config, "MX_DEFAULT_AUDIO", "ask")).lower() == "all":
            self.selected_audio.update(range(len(audios)))
        buttons = ButtonMaker()
        for index, item in enumerate(audios[:25]):
            mark = "[x]" if index in self.selected_audio else "[ ]"
            buttons.data_button(f"{mark} {item['label']}", f"ytq site_mxa {index}")
        buttons.data_button("All Audio", "ytq site_mxaall")
        buttons.data_button("Skip Audio", "ytq site_mxaskip")
        buttons.data_button("Download", "ytq site_mxadone", "footer")
        buttons.data_button("Back", "ytq site_mxaback", "footer")
        text = (
            f"Choose MX Player audio for:\n<b>{self.site_data['title']}</b>\n"
            f"Select multiple tracks for multi-audio.\nTimeout: {self._timeout_text()}"
        )
        await edit_message(self._reply_to, text, buttons.build_menu(2))

    async def _show_hanime_streams(self):
        buttons = ButtonMaker()
        streams = self.site_data.get("streams", [])
        if not self.selected_videos and streams:
            self.selected_videos.add(0)
        for index, item in enumerate(streams[:25]):
            mark = "[x]" if index in self.selected_videos else "[ ]"
            buttons.data_button(f"{mark} {item['label']}", f"ytq site_hv {index}")
        buttons.data_button("All Qualities", "ytq site_hvall")
        buttons.data_button("Download", "ytq site_hdone", "footer")
        buttons.data_button("Cancel", "ytq cancel", "footer")
        text = (
            f"Choose Hanime quality for:\n<b>{self.site_data['title']}</b>\n"
            f"Tap multiple qualities if needed.\nTimeout: {self._timeout_text()}"
        )
        menu = buttons.build_menu(2)
        if self._reply_to:
            await edit_message(self._reply_to, text, menu)
        else:
            self._reply_to = await send_message(self.listener.message, text, menu)

    async def site_callback(self, data):
        action = data[0]
        if action == "site_mxv":
            index = int(data[1])
            if index in self.selected_videos:
                self.selected_videos.remove(index)
            else:
                self.selected_videos.add(index)
            if not self.selected_videos:
                self.selected_videos.add(index)
            await self._show_mx_videos()
        elif action == "site_mxvall":
            self.selected_videos = set(range(len(self.site_data.get("videos", []))))
            await self._show_mx_videos()
        elif action == "site_mxnext":
            await self._show_mx_audio()
        elif action == "site_mxa":
            index = int(data[1])
            if index in self.selected_audio:
                self.selected_audio.remove(index)
            else:
                self.selected_audio.add(index)
            await self._show_mx_audio()
        elif action == "site_mxaall":
            self.selected_audio = set(range(len(self.site_data.get("audios", []))))
            await self._show_mx_audio()
        elif action == "site_mxaskip":
            self.selected_audio.clear()
            self.qual = self._build_mx_result(
                [self.site_data["videos"][idx] for idx in sorted(self.selected_videos)],
                [],
            )
            self.event.set()
        elif action == "site_mxadone":
            self.qual = self._build_mx_result(
                [self.site_data["videos"][idx] for idx in sorted(self.selected_videos)],
                [
                    self.site_data["audios"][idx]["id"]
                    for idx in sorted(self.selected_audio)
                ],
            )
            self.event.set()
        elif action == "site_mxaback":
            await self._show_mx_videos()
        elif action == "site_hv":
            index = int(data[1])
            if index in self.selected_videos:
                self.selected_videos.remove(index)
            else:
                self.selected_videos.add(index)
            if not self.selected_videos:
                self.selected_videos.add(index)
            await self._show_hanime_streams()
        elif action == "site_hvall":
            self.selected_videos = set(range(len(self.site_data.get("streams", []))))
            await self._show_hanime_streams()
        elif action == "site_hdone":
            self.qual = self._build_hanime_result(
                [self.site_data["streams"][idx] for idx in sorted(self.selected_videos)]
            )
            self.event.set()

    def _build_mx_result(self, videos, audio_ids):
        if not videos:
            videos = self.site_data.get("videos", [])[:1]
        audio_suffix = f"+{'+'.join(audio_ids)}" if audio_ids else ""
        formats = [f"{video['id']}{audio_suffix}" for video in videos]
        return {
            "link": self.site_data["download_url"],
            "qual": ",".join(formats) if formats else "best",
            "name": self.site_data["title"],
            "thumb": self.site_data.get("thumbnail") or "",
            "site": "mx",
            "options": {"allow_multiple_audio_streams": len(audio_ids) > 1},
        }

    def _build_hanime_result(self, streams):
        if not streams:
            streams = self.site_data.get("streams", [])[:1]
        links = [item["url"] for item in streams]
        return {
            "link": links[0] if len(links) == 1 else links,
            "qual": "best",
            "name": self.site_data["title"],
            "thumb": self.site_data.get("thumbnail") or "",
            "site": "hanime",
            "options": {},
        }


def extract_info(link, options):
    with YoutubeDL(options) as ydl:
        result = ydl.extract_info(link, download=False)
        if result is None:
            raise ValueError("Info result is None")
        return result


async def _mdisk(link, name):
    key = link.split("/")[-1]
    async with AsyncClient() as client:
        resp = await client.get(
            f"https://diskuploader.entertainvideo.com/v1/file/cdnurl?param={key}"
        )
    if resp.status_code == 200:
        resp_json = resp.json()
        link = resp_json["source"]
        if not name:
            name = resp_json["filename"]
    return name, link


class YtDlp(TaskListener):
    def __init__(
        self,
        client,
        message,
        is_leech=False,
        same_dir=None,
        bulk=None,
        multi_tag=None,
        options="",
        **kwargs,
    ):
        if same_dir is None:
            same_dir = {}
        if bulk is None:
            bulk = []
        self.message = message
        self.client = client
        self.multi_tag = multi_tag
        self.options = options
        self.same_dir = same_dir
        self.bulk = bulk
        super().__init__()
        self.is_ytdlp = True
        self.is_leech = is_leech

    async def new_event(self):
        text = self.message.text.split("\n")
        input_list = text[0].split(" ")
        qual = ""

        check_msg, check_button = await pre_task_check(self.message)
        if check_msg:
            await delete_links(self.message)
            await auto_delete_message(
                await send_message(self.message, check_msg, check_button)
            )
            return

        args = {
            "-doc": False,
            "-med": False,
            "-s": False,
            "-b": False,
            "-z": False,
            "-sv": False,
            "-ss": False,
            "-f": False,
            "-fd": False,
            "-fu": False,
            "-hl": False,
            "-bt": False,
            "-ut": False,
            "-i": 0,
            "-sp": 0,
            "link": "",
            "-m": "",
            "-meta": "",
            "-opt": {},
            "-n": "",
            "-up": "",
            "-gc": "",
            "-rcf": "",
            "-t": "",
            "-ca": "",
            "-cv": "",
            "-ns": "",
            "-tl": "",
            "-ff": set(),
        }

        arg_parser(input_list[1:], args)

        if Config.DISABLE_FF_MODE and args.get("-ff"):
            await send_message(self.message, "FFmpeg commands are currently disabled.")
            return

        try:
            self.multi = int(args["-i"])
        except Exception:
            self.multi = 0

        try:
            if args["-ff"]:
                if isinstance(args["-ff"], set):
                    self.ffmpeg_cmds = args["-ff"]
                else:
                    value = literal_eval(args["-ff"])
                    if not isinstance(value, (dict, set, list, tuple)):
                        raise ValueError("ffmpeg_cmds must be a dict/set/list/tuple")
                    self.ffmpeg_cmds = value
        except Exception as e:
            self.ffmpeg_cmds = None
            LOGGER.error(e)

        try:
            opt = literal_eval(args["-opt"]) if args["-opt"] else {}
            if not isinstance(opt, dict):
                raise ValueError("yt-dlp options must be a dict")
        except Exception as e:
            LOGGER.error(e)
            opt = {}

        self.select = args["-s"]
        self.name = args["-n"]
        self.custom_name = args["-n"]
        self.up_dest = args["-up"]
        self.category = args["-gc"]
        self.rc_flags = args["-rcf"]
        self.link = args["link"]
        self.compress = args["-z"]
        self.thumb = args["-t"]
        self.split_size = args["-sp"]
        self.sample_video = args["-sv"]
        self.screen_shots = args["-ss"]
        self.force_run = args["-f"]
        self.force_download = args["-fd"]
        self.force_upload = args["-fu"]
        self.convert_audio = args["-ca"]
        self.convert_video = args["-cv"]
        self.name_swap = args["-ns"]
        self.hybrid_leech = args["-hl"]
        self.thumbnail_layout = args["-tl"]
        self.as_doc = args["-doc"]
        self.as_med = args["-med"]
        self.folder_name = f"/{args['-m']}".rstrip("/") if len(args["-m"]) > 0 else ""
        self.bot_trans = args["-bt"]
        self.user_trans = args["-ut"]
        self.metadata_dict = self.default_metadata_dict.copy()
        self.audio_metadata_dict = self.audio_metadata_dict.copy()
        self.video_metadata_dict = self.video_metadata_dict.copy()
        self.subtitle_metadata_dict = self.subtitle_metadata_dict.copy()
        if meta := args["-meta"]:
            self.metadata_dict = self.metadata_processor.merge_dicts(
                self.default_metadata_dict, self.metadata_processor.parse_string(meta)
            )

        is_bulk = args["-b"]

        bulk_start = 0
        bulk_end = 0
        reply_to = None

        if not isinstance(is_bulk, bool):
            dargs = is_bulk.split(":")
            bulk_start = dargs[0] or None
            if len(dargs) == 2:
                bulk_end = dargs[1] or None
            is_bulk = True

        if not is_bulk:
            if self.multi > 0:
                if self.folder_name:
                    async with task_dict_lock:
                        if self.folder_name in self.same_dir:
                            self.same_dir[self.folder_name]["tasks"].add(self.mid)
                            for fd_name in self.same_dir:
                                if fd_name != self.folder_name:
                                    self.same_dir[fd_name]["total"] -= 1
                        elif self.same_dir:
                            self.same_dir[self.folder_name] = {
                                "total": self.multi,
                                "tasks": {self.mid},
                            }
                            for fd_name in self.same_dir:
                                if fd_name != self.folder_name:
                                    self.same_dir[fd_name]["total"] -= 1
                        else:
                            self.same_dir = {
                                self.folder_name: {
                                    "total": self.multi,
                                    "tasks": {self.mid},
                                }
                            }
                elif self.same_dir:
                    async with task_dict_lock:
                        for fd_name in self.same_dir:
                            self.same_dir[fd_name]["total"] -= 1
        else:
            await self.init_bulk(input_list, bulk_start, bulk_end, YtDlp)
            return

        if len(self.bulk) != 0:
            del self.bulk[0]

        path = f"{DOWNLOAD_DIR}{self.mid}{self.folder_name}"

        await self.get_tag(text)

        opt = opt or self.user_dict.get("YT_DLP_OPTIONS") or Config.YT_DLP_OPTIONS

        if not self.link and (reply_to := self.message.reply_to_message):
            if reply_to.text:
                self.link = reply_to.text.split("\n", 1)[0].strip()

        if not is_url(self.link):
            await send_message(
                self.message, COMMAND_USAGE["yt"][0], COMMAND_USAGE["yt"][1]
            )
            await self.remove_from_same_dir()
            await delete_links(self.message)
            return

        if "mdisk.me" in self.link:
            self.name, self.link = await _mdisk(self.link, self.name)

        try:
            await self.before_start()
        except Exception as e:
            await send_message(self.message, e)
            await self.remove_from_same_dir()
            await delete_links(self.message)
            return

        self._set_mode_engine()

        cookie_to_use = (
            usr_cookie
            if not self.user_dict.get("USE_DEFAULT_COOKIE", False)
            and (usr_cookie := self.user_dict.get("USER_COOKIE_FILE", ""))
            and await aiopath.exists(usr_cookie)
            else "cookies.txt"
        )
        LOGGER.info(
            f"Using cookies.txt file: {cookie_to_use} | User ID : {self.user_id}"
        )

        download_opt = {
            key: value for key, value in opt.items() if key not in SITE_OPTION_KEYS
        }

        options = {"usenetrc": True, "cookiefile": cookie_to_use}
        if opt:
            for key, value in opt.items():
                if key in SITE_OPTION_KEYS:
                    continue
                if key in ["postprocessors", "download_ranges"]:
                    continue
                if key == "format" and not self.select:
                    if value.startswith("ba/b-"):
                        qual = value
                        continue
                    else:
                        qual = value
                options[key] = value
        options["playlist_items"] = "0"
        site_data = None
        try:
            site_data = await resolve_external_site(self.link, opt)
        except Exception as e:
            msg = str(e).replace("<", " ").replace(">", " ")
            await send_message(self.message, f"{self.tag} {msg}")
            await self.remove_from_same_dir()
            await delete_links(self.message)
            return

        if site_data:
            selected = await SiteSelection(self, site_data, opt).get_quality()
            if selected is None:
                await self.remove_from_same_dir()
                return
            self.link = selected["link"]
            qual = selected["qual"]
            if selected.get("site") == "hanime":
                self.skip_auto_rename = True
                self.skip_auto_thumbnail = True
            if selected.get("name") and not self.name:
                self.name = selected["name"]
                self.custom_name = selected["name"]
            if selected.get("thumb") and not self.thumb:
                thumb = selected["thumb"]
                if isinstance(thumb, str) and thumb.startswith(("http://", "https://")):
                    thumb = await download_image_thumb(thumb, landscape=True)
                if thumb:
                    self.thumb = thumb
            if selected.get("options"):
                download_opt = {**download_opt, **selected["options"]}
            await self.run_multi(input_list, YtDlp)
        else:
            try:
                result = await sync_to_async(extract_info, self.link, options)
            except Exception as e:
                msg = str(e).replace("<", " ").replace(">", " ")
                await send_message(self.message, f"{self.tag} {msg}")
                await self.remove_from_same_dir()
                await delete_links(self.message)
                return
            finally:
                await self.run_multi(input_list, YtDlp)

            if not qual:
                qual = await YtSelection(self).get_quality(result)
                if qual is None:
                    await self.remove_from_same_dir()
                    return

        LOGGER.info(f"Downloading with YT-DLP: {self.link}")
        playlist = bool(site_data) and isinstance(self.link, list)
        if not site_data:
            playlist = "entries" in result

        ydl = YoutubeDLHelper(self)
        await delete_links(self.message)
        await ydl.add_download(path, qual, playlist, download_opt)


async def ytdl(client, message):
    if Config.DISABLE_YTDLP:
        await message.reply("YT-DLP downloads are currently disabled by the Bot Owner.")
        return
    bot_loop.create_task(YtDlp(client, message).new_event())


async def ytdl_leech(client, message):
    if Config.DISABLE_YTDLP:
        await message.reply("YT-DLP downloads are currently disabled by the Bot Owner.")
        return
    bot_loop.create_task(YtDlp(client, message, is_leech=True).new_event())
