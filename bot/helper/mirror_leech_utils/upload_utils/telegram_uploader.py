from asyncio import create_task, sleep
from logging import getLogger
from os import path as ospath, walk
from re import IGNORECASE, match as re_match, sub as re_sub
from time import time

from aioshutil import rmtree
from natsort import natsorted
from PIL import Image
from pyrogram.errors import BadRequest, FloodWait, RPCError

try:
    from pyrogram.errors import FloodPremiumWait
except ImportError:
    FloodPremiumWait = FloodWait
from aiofiles.os import (
    path as aiopath,
    remove,
    rename,
)
from pyrogram.types import (
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ....core.config_manager import Config
from ....core.tg_client import TgClient
from ...ext_utils.bot_utils import sync_to_async
from ...ext_utils.files_utils import get_base_name, is_archive
from ...ext_utils.performance import get_tg_copy_delay, get_tg_flood_wait_multiplier
from ...ext_utils.starfallx_upload import (
    private_dump_only,
    starfallx_upload,
)
from ...ext_utils.status_utils import get_readable_file_size, get_readable_time
from ...telegram_helper.message_utils import send_message
from ...ext_utils.media_utils import (
    apply_regex_rename,
    apply_template_rename,
    build_caption_metadata,
    download_image_thumb,
    get_anime_landscape_thumbnail,
    get_audio_thumbnail,
    get_document_type,
    get_final_poster_url,
    get_telegram_document_thumb,
    get_media_info,
    get_multiple_frames_thumbnail,
    get_video_thumbnail,
)
from ...telegram_helper.message_utils import delete_message

LOGGER = getLogger(__name__)


class TelegramUploader:
    def __init__(self, listener, path):
        self._last_uploaded = 0
        self._processed_bytes = 0
        self._listener = listener
        self._path = path
        self._client = None
        self._start_time = time()
        self._total_files = 0
        self._thumb = self._listener.thumb or f"thumbnails/{listener.user_id}.jpg"
        self._msgs_dict = {}
        self._corrupted = 0
        self._is_corrupted = False
        self._media_dict = {"videos": {}, "documents": {}}
        self._last_msg_in_group = False
        self._up_path = ""
        self._lprefix = ""
        self._lsuffix = ""
        self._lcaption = ""
        self._lfont = ""
        self._complete_msg = True
        self._sequential_leech = True
        self._bot_pm = False
        self._media_group = False
        self._is_private = False
        self._sent_msg = None
        self._log_msg = None
        self._user_session = self._listener.user_transmission
        self._error = ""
        self._deferred_copies = []
        self._active_route = None
        self._private_dump_only = False
        self._private_dump_warned = False

    async def _upload_progress(self, current, _):
        if self._listener.is_cancelled:
            if self._active_route and self._active_route.direct:
                self._active_route.client.stop_transmission()
            elif self._user_session:
                TgClient.user.stop_transmission()
            else:
                self._listener.client.stop_transmission()
        chunk_size = current - self._last_uploaded
        self._last_uploaded = current
        self._processed_bytes += chunk_size

    async def _user_settings(self):
        settings_map = {
            "MEDIA_GROUP": ("_media_group", False),
            "BOT_PM": ("_bot_pm", False),
            "LEECH_PREFIX": ("_lprefix", ""),
            "LEECH_SUFFIX": ("_lsuffix", ""),
            "LEECH_CAPTION": ("_lcaption", ""),
            "LEECH_FONT": ("_lfont", ""),
            "LEECH_COMPLETE_MSG": ("_complete_msg", True),
            "SEQUENTIAL_LEECH": ("_sequential_leech", True),
        }

        for key, (attr, default) in settings_map.items():
            setattr(
                self,
                attr,
                self._listener.user_dict.get(key) or getattr(Config, key, default),
            )

        if self._thumb != "none" and not await aiopath.exists(self._thumb):
            self._thumb = None

    async def _msg_to_reply(self):
        if self._listener.up_dest:
            msg_link = (
                self._listener.message.link if self._listener.is_super_chat else ""
            )
            msg = f"""➲ <b><u>Leech Started :</u></b>
┃
┠ <b>User :</b> {self._listener.user.mention} ( #ID{self._listener.user_id} ){f"\n┠ <b>Message Link :</b> <a href='{msg_link}'>Click Here</a>" if msg_link else ""}
┖ <b>Source :</b> <a href='{self._listener.source_url}'>Click Here</a>"""
            try:
                await TgClient.bot.resolve_peer(self._listener.up_dest)
                self._log_msg = await TgClient.bot.send_message(
                    chat_id=self._listener.up_dest,
                    text=msg,
                    disable_web_page_preview=True,
                    message_thread_id=self._listener.chat_thread_id,
                    disable_notification=True,
                )
                self._sent_msg = self._log_msg
                if self._user_session:
                    self._sent_msg = await TgClient.user.get_messages(
                        chat_id=self._sent_msg.chat.id,
                        message_ids=self._sent_msg.id,
                    )
                else:
                    self._is_private = self._sent_msg.chat.type.name == "PRIVATE"
                if self._listener.leech_dest:
                    try:
                        leech_dest = self._listener.leech_dest
                        if not isinstance(leech_dest, int):
                            if "|" in str(leech_dest):
                                leech_dest, _ = str(leech_dest).split("|", 1)
                            if leech_dest.lstrip("-").isdigit():
                                leech_dest = int(leech_dest)
                        await self._log_msg.copy(chat_id=leech_dest)
                    except Exception as e:
                        if not self._listener.is_cancelled:
                            LOGGER.error(
                                f"Failed to copy 'Leech Started' message to {self._listener.leech_dest}: {e}"
                            )
                            await send_message(
                                self._listener.user_id,
                                f"Failed to send 'Leech Started' message to {self._listener.leech_dest}\n{e}",
                            )
            except Exception as e:
                await self._listener.on_upload_error(str(e))
                return False

        elif self._user_session:
            self._sent_msg = await TgClient.user.get_messages(
                chat_id=self._listener.message.chat.id, message_ids=self._listener.mid
            )
            if self._sent_msg is None:
                self._sent_msg = await TgClient.user.send_message(
                    chat_id=self._listener.message.chat.id,
                    text="Deleted Cmd Message! Don't delete the cmd message again!",
                    disable_web_page_preview=True,
                    disable_notification=True,
                )
        else:
            self._sent_msg = self._listener.message
        return True

    async def _prepare_file(self, pre_file_, dirpath):
        cap_file_ = file_ = pre_file_
        # AutoRename logic: apply before prefix/suffix
        autorename_enabled = (
            self._listener.user_dict.get("AUTORENAME")
            if "AUTORENAME" in self._listener.user_dict
            else Config.AUTORENAME
        )
        rename_method = (
            self._listener.user_dict.get("RENAME_METHOD")
            or Config.RENAME_METHOD
        )

        auto_process_enabled = (
            self._listener.user_dict.get("AUTO_PROCESS")
            if "AUTO_PROCESS" in self._listener.user_dict
            else getattr(Config, "AUTO_PROCESS", False)
        )
        if auto_process_enabled:
            autorename_enabled = bool(autorename_enabled)
        if getattr(self._listener, "skip_auto_rename", False):
            autorename_enabled = False

        merge_range_name = re_match(r"^\[S\d+-EP\(\d+-\d+\)\]", file_, IGNORECASE)

        if autorename_enabled and not merge_range_name:
            try:
                if rename_method == "auto":
                    template = (
                        self._listener.user_dict.get("lremname_auto")
                        or Config.LEECH_FILENAME_REMNAME_AUTO
                    )
                    if template:
                        file_ = await apply_template_rename(
                            file_,
                            template,
                            self._up_path,
                            file_caption=getattr(self._listener, "file_details", {}).get("caption", ""),
                            link=getattr(self._listener, "source_url", ""),
                        )
                        cap_file_ = file_
                elif rename_method == "regex":
                    pattern = (
                        self._listener.user_dict.get("lremname_regex")
                        or Config.LEECH_FILENAME_REMNAME_REGEX
                    )
                    if pattern:
                        file_ = apply_regex_rename(file_, pattern)
                        cap_file_ = file_
            except Exception as e:
                LOGGER.warning(f"AutoRename failed for {pre_file_}: {e}")

        if self._lprefix:
            cap_file_ = self._lprefix.replace(r"\s", " ") + file_
            self._lprefix = re_sub(r"<.*?>", "", self._lprefix).replace(r"\s", " ")
            if not file_.startswith(self._lprefix):
                file_ = f"{self._lprefix}{file_}"

        if self._lsuffix:
            name, ext = ospath.splitext(cap_file_)
            cap_file_ = name + self._lsuffix.replace(r"\s", " ") + ext
            self._lsuffix = re_sub(r"<.*?>", "", self._lsuffix).replace(r"\s", " ")

        cap_mono = (
            f"<{self._lfont}>{cap_file_}</{self._lfont}>"
            if self._lfont
            else cap_file_
        )
        if self._lcaption:
            caption_template = re_sub(
                r"(\\\||\\\{|\\\}|\\s)",
                lambda m: {r"\|": "%%", r"\{": "&%&", r"\}": "$%$", r"\s": " "}[
                    m.group(0)
                ],
                self._lcaption,
            )

            parts = caption_template.split("|")
            parts[0] = re_sub(
                r"\{([^}]+)\}", lambda m: f"{{{m.group(1).lower()}}}", parts[0]
            )
            up_path = self._up_path or ospath.join(dirpath, pre_file_)
            caption_data = await build_caption_metadata(
                cap_file_,
                up_path,
                mime_type=self._listener.file_details.get("mime_type", "text/plain"),
                upload_filename=file_,
                prefilename=self._listener.file_details.get("filename", ""),
                precaption=self._listener.file_details.get("caption", ""),
                file_caption=self._listener.file_details.get("caption", ""),
                link=getattr(self._listener, "source_url", ""),
            )
            try:
                cap_mono = parts[0].format_map(caption_data)
            except Exception as e:
                LOGGER.warning(f"Caption format failed for {pre_file_}: {e}")
                cap_mono = cap_file_

            for part in parts[1:]:
                args = part.split(":")
                cap_mono = cap_mono.replace(
                    args[0],
                    args[1] if len(args) > 1 else "",
                    int(args[2]) if len(args) == 3 else -1,
                )
            cap_mono = re_sub(
                r"%%|&%&|\$%\$",
                lambda m: {"%%": "|", "&%&": "{", "$%$": "}"}[m.group()],
                cap_mono,
            )

        if len(file_) > 255:
            if is_archive(file_):
                name = get_base_name(file_)
                ext = file_.split(name, 1)[1]
            elif match := re_match(r".+(?=\..+\.0*\d+$)|.+(?=\.part\d+\..+$)", file_):
                name = match.group(0)
                ext = file_.split(name, 1)[1]
            elif len(fsplit := ospath.splitext(file_)) > 1:
                name = fsplit[0]
                ext = fsplit[1]
            else:
                name = file_
                ext = ""
            if self._lsuffix:
                ext = f"{self._lsuffix}{ext}"
            name = name[: 255 - len(ext)]
            file_ = f"{name}{ext}"
        elif self._lsuffix:
            name, ext = ospath.splitext(file_)
            file_ = f"{name}{self._lsuffix}{ext}"

        if pre_file_ != file_:
            new_path = ospath.join(dirpath, file_)
            await rename(self._up_path, new_path)
            self._up_path = new_path

        return cap_mono

    def _get_input_media(self, subkey, key):
        rlist = []
        for msg in self._media_dict[key][subkey]:
            if key == "videos":
                input_media = InputMediaVideo(
                    media=msg.video.file_id, caption=msg.caption
                )
            else:
                input_media = InputMediaDocument(
                    media=msg.document.file_id, caption=msg.caption
                )
            rlist.append(input_media)
        return rlist

    async def _send_screenshots(self, dirpath, outputs):
        inputs = [
            InputMediaPhoto(ospath.join(dirpath, p), p.rsplit("/", 1)[-1])
            for p in outputs
        ]
        for i in range(0, len(inputs), 10):
            batch = inputs[i : i + 10]
            if Config.BOT_PM:
                await TgClient.bot.send_media_group(
                    chat_id=self._listener.user_id,
                    media=batch,
                    disable_notification=True,
                )
            self._sent_msg = (
                await self._sent_msg.reply_media_group(
                    media=batch,
                    quote=True,
                    disable_notification=True,
                )
            )[-1]

    async def _send_auto_post(self):
        post = getattr(self._listener, "auto_post", None)
        if not post:
            return
        path = post.get("path")
        if not path or not await aiopath.exists(path):
            return
        try:
            caption = post.get("caption") or "<b>Poster</b>"
            target = self._sent_msg or self._listener.message
            sent = await send_message(target, caption, photo=path)
            if sent:
                self._sent_msg = sent
                if (
                    (self._listener.is_super_chat or self._listener.up_dest)
                    and not self._is_private
                ):
                    self._queue_deferred_copy(sent)
        except Exception as err:
            LOGGER.warning(f"Failed to send auto poster: {err}", exc_info=True)

    async def _send_media_group(self, subkey, key, msgs):
        for index, msg in enumerate(msgs):
            if self._listener.hybrid_leech or not self._user_session:
                msgs[index] = await self._listener.client.get_messages(
                    chat_id=msg[0], message_ids=msg[1]
                )
            else:
                msgs[index] = await TgClient.user.get_messages(
                    chat_id=msg[0], message_ids=msg[1]
                )
        msgs_list = await msgs[0].reply_to_message.reply_media_group(
            media=self._get_input_media(subkey, key),
            quote=True,
            disable_notification=True,
        )
        for msg in msgs:
            if msg.link in self._msgs_dict:
                del self._msgs_dict[msg.link]
            await delete_message(msg)
        del self._media_dict[key][subkey]
        if self._listener.is_super_chat or self._listener.up_dest:
            for m in msgs_list:
                self._msgs_dict[m.link] = m.caption
                self._queue_deferred_copy(m)
        self._sent_msg = msgs_list[-1]


    def _queue_deferred_copy(self, msg):
        if not msg:
            return
        if self._private_dump_only:
            if not self._private_dump_warned:
                self._private_dump_warned = True
                create_task(
                    send_message(
                        self._listener.user_id,
                        "StarFallX: this task matched private-dump-only filters, so forwarding to user PM/dump was skipped.",
                    )
                )
            return
        if self._bot_pm or self._listener.leech_dest:
            if self._sequential_leech:
                self._deferred_copies.append((msg.chat.id, msg.id))
            else:
                create_task(self._copy_deferred_message(msg.chat.id, msg.id))

    def _get_leech_dest(self):
        leech_dest = self._listener.leech_dest
        thread_id = None
        if not leech_dest:
            return None, None
        if not isinstance(leech_dest, int):
            if "|" in str(leech_dest):
                leech_dest, thread_id = str(leech_dest).split("|", 1)
                thread_id = int(thread_id) if thread_id.lstrip("-").isdigit() else None
            if str(leech_dest).lstrip("-").isdigit():
                leech_dest = int(leech_dest)
        return leech_dest, thread_id

    async def _copy_message_with_backoff(self, **kwargs):
        try:
            return await TgClient.bot.copy_message(**kwargs)
        except (FloodWait, FloodPremiumWait) as f:
            wait_time = max(f.value + 1, f.value * get_tg_flood_wait_multiplier())
            LOGGER.warning(f"Copy flood wait: sleeping {wait_time:.1f}s")
            await sleep(wait_time)
            return await TgClient.bot.copy_message(**kwargs)

    async def _flush_deferred_copies(self):
        if not self._deferred_copies:
            return
        for chat_id, msg_id in self._deferred_copies:
            if self._listener.is_cancelled:
                return
            await self._copy_deferred_message(chat_id, msg_id)
            await sleep(get_tg_copy_delay())

    async def _copy_deferred_message(self, chat_id, msg_id):
        leech_dest, thread_id = self._get_leech_dest()
        if self._bot_pm:
            try:
                await self._copy_message_with_backoff(
                    chat_id=self._listener.user_id,
                    from_chat_id=chat_id,
                    message_id=msg_id,
                    reply_to_message_id=(
                        self._listener.pm_msg.id
                        if self._listener.pm_msg
                        else None
                    ),
                )
            except Exception as err:
                LOGGER.error(f"Failed To Send in BotPM:\n{str(err)}")
        if leech_dest:
            try:
                await self._copy_message_with_backoff(
                    chat_id=leech_dest,
                    from_chat_id=chat_id,
                    message_id=msg_id,
                    message_thread_id=thread_id,
                )
            except Exception as e:
                LOGGER.error(f"Failed to forward to {leech_dest}: {e}")
                await send_message(
                    self._listener.user_id,
                    f"Failed to forward to {leech_dest}\n{e}",
                )

    async def upload(self):
        await self._user_settings()
        res = await self._msg_to_reply()
        if not res:
            return
        await self._send_auto_post()
        is_log_del = False
        for dirpath, _, files in natsorted(await sync_to_async(walk, self._path)):
            if dirpath.strip().endswith("/yt-dlp-thumb"):
                continue
            if dirpath.strip().endswith("_mltbss"):
                await self._send_screenshots(dirpath, files)
                await rmtree(dirpath, ignore_errors=True)
                continue
            for file_ in natsorted(files):
                self._error = ""
                self._up_path = f_path = ospath.join(dirpath, file_)
                if not await aiopath.exists(self._up_path):
                    LOGGER.warning(f"{self._up_path} not found; skipping upload.")
                    continue
                try:
                    f_size = await aiopath.getsize(self._up_path)
                    self._total_files += 1
                    if f_size == 0:
                        LOGGER.error(
                            f"{self._up_path} size is zero, telegram don't upload zero size files"
                        )
                        self._corrupted += 1
                        continue
                    if self._listener.is_cancelled:
                        return
                    cap_mono = await self._prepare_file(file_, dirpath)
                    f_path = self._up_path
                    file_ = ospath.basename(self._up_path)
                    if self._last_msg_in_group:
                        group_lists = [
                            x for v in self._media_dict.values() for x in v.keys()
                        ]
                        match = re_match(r".+(?=\.0*\d+$)|.+(?=\.part\d+\..+$)", f_path)
                        if not match or match and match.group(0) not in group_lists:
                            for key, value in list(self._media_dict.items()):
                                for subkey, msgs in list(value.items()):
                                    if len(msgs) > 1:
                                        await self._send_media_group(subkey, key, msgs)
                    if self._listener.hybrid_leech and self._listener.user_transmission:
                        self._user_session = f_size > 2097152000
                        if self._user_session:
                            self._sent_msg = await TgClient.user.get_messages(
                                chat_id=self._sent_msg.chat.id,
                                message_ids=self._sent_msg.id,
                            )
                        else:
                            self._sent_msg = await self._listener.client.get_messages(
                                chat_id=self._sent_msg.chat.id,
                                message_ids=self._sent_msg.id,
                            )
                    self._last_msg_in_group = False
                    self._last_uploaded = 0
                    await self._upload_file(cap_mono, file_, f_path)
                    if self._log_msg and not is_log_del and Config.CLEAN_LOG_MSG:
                        await delete_message(self._log_msg)
                        is_log_del = True
                    if self._listener.is_cancelled:
                        return
                    if (
                        not self._is_corrupted
                        and (self._listener.is_super_chat or self._listener.up_dest)
                        and not self._is_private
                    ):
                        self._msgs_dict[self._sent_msg.link] = file_
                    await sleep(1)
                except Exception as err:
                    if isinstance(err, RetryError):
                        LOGGER.info(
                            f"Total Attempts: {err.last_attempt.attempt_number}"
                        )
                        err = err.last_attempt.exception()
                    LOGGER.error(f"{err}. Path: {self._up_path}", exc_info=True)
                    self._error = str(err)
                    self._corrupted += 1
                    if self._listener.is_cancelled:
                        return
                if not self._listener.is_cancelled and await aiopath.exists(
                    self._up_path
                ):
                    await remove(self._up_path)
        for key, value in list(self._media_dict.items()):
            for subkey, msgs in list(value.items()):
                if len(msgs) > 1:
                    try:
                        await self._send_media_group(subkey, key, msgs)
                    except Exception as e:
                        LOGGER.info(
                            f"While sending media group at the end of task. Error: {e}"
                        )
        if self._listener.is_cancelled:
            return
        if self._total_files == 0:
            await self._listener.on_upload_error(
                "No files to upload. In case you have filled EXCLUDED_EXTENSIONS, then check if all files have those extensions or not."
            )
            return
        if self._total_files <= self._corrupted:
            await self._listener.on_upload_error(
                f"Files Corrupted or unable to upload. {self._error or 'Check logs!'}"
            )
            return
        await self._flush_deferred_copies()
        if self._listener.is_cancelled:
            return
        LOGGER.info(f"Leech Completed: {self._listener.name}")
        await self._listener.on_upload_complete(
            None, self._msgs_dict, self._total_files, self._corrupted
        )
        return

    @retry(
        wait=wait_exponential(multiplier=2, min=4, max=8),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    async def _send_direct_file(
        self,
        route,
        key,
        cap_mono,
        thumb=None,
        duration=0,
        width=480,
        height=320,
        artist=None,
        title=None,
    ):
        common = {
            "chat_id": route.chat_id,
            "caption": cap_mono,
            "disable_notification": True,
            "message_thread_id": route.thread_id,
            "progress": self._upload_progress,
        }
        if key == "documents":
            return await route.client.send_document(
                document=self._up_path,
                thumb=thumb,
                disable_content_type_detection=True,
                **common,
            )
        if key == "videos":
            return await route.client.send_video(
                video=self._up_path,
                duration=duration,
                width=width,
                height=height,
                thumb=thumb,
                cover=thumb,
                supports_streaming=True,
                **common,
            )
        if key == "audios":
            return await route.client.send_audio(
                audio=self._up_path,
                duration=duration,
                performer=artist,
                title=title,
                thumb=thumb,
                **common,
            )
        return await route.client.send_photo(
            photo=self._up_path,
            **common,
        )

    async def _upload_file(self, cap_mono, file, o_path, force_document=False):
        if not await aiopath.exists(o_path):
            LOGGER.warning(f"{o_path} disappeared before upload; skipping.")
            self._is_corrupted = True
            return

        if self._sent_msg is None:
            LOGGER.error("Cannot upload: _sent_msg is None")
            await self._listener.on_upload_error(
                "Upload failed: Message not initialized"
            )
            return

        if not hasattr(self._sent_msg, "chat") or self._sent_msg.chat is None:
            LOGGER.error("Cannot upload: _sent_msg.chat is None")
            await self._listener.on_upload_error(
                "Upload failed: Invalid message object"
            )
            return

        if (
            self._thumb is not None
            and not await aiopath.exists(self._thumb)
            and self._thumb != "none"
        ):
            self._thumb = None
        thumb = self._thumb
        doc_thumb = None
        self._is_corrupted = False
        route = None
        key = ""
        try:
            is_video, is_audio, is_image = await get_document_type(self._up_path)
            queued_for_media_group = False

            # Auto thumbnail order: custom thumb, TMDb poster/backdrop, then local media.
            if not is_image and thumb is None:
                auto_thumb_enabled = (
                    self._listener.user_dict.get("AUTO_THUMBNAIL")
                    if "AUTO_THUMBNAIL" in self._listener.user_dict
                    else Config.AUTO_THUMBNAIL
                )
                if getattr(self._listener, "skip_auto_thumbnail", False):
                    auto_thumb_enabled = False
                if auto_thumb_enabled:
                    LOGGER.info(f"Auto-thumbnail enabled for: {file}")
                    try:
                        as_doc = self._listener.as_doc
                        custom_name = getattr(self._listener, "custom_name", "")
                        thumb_lookup_name = custom_name or file
                        force_anime_thumb = getattr(self._listener, "force_anime_thumbnail", False)
                        rename_regex = (
                            self._listener.user_dict.get("lremname_regex")
                            or Config.LEECH_FILENAME_REMNAME_REGEX
                        )
                        if is_video:
                            thumb = await get_anime_landscape_thumbnail(
                                self._up_path,
                                thumb_lookup_name,
                                None,
                                rename_regex,
                                force_anime_thumb,
                            )
                        if thumb is None:
                            poster_url = await get_final_poster_url(
                                custom_name or file, as_doc, rename_regex
                            )
                            if poster_url:
                                tmdb_thumb = await download_image_thumb(
                                    poster_url, landscape=not as_doc
                                )
                                if tmdb_thumb:
                                    thumb = tmdb_thumb
                        if thumb:
                            LOGGER.info(f"Auto-thumbnail selected: {thumb}")
                        else:
                            LOGGER.info(f"Auto-thumbnail provider lookup found no image for: {file}")
                    except Exception as e:
                        LOGGER.warning(f"Auto-thumbnail failed: {e}")
                else:
                    LOGGER.info(f"Auto-thumbnail disabled for: {file}")
            elif not is_image and thumb:
                LOGGER.info(f"Using custom thumbnail for: {file}")

            if not is_image and thumb is None:
                file_name = ospath.splitext(file)[0]
                thumb_path = f"{self._path}/yt-dlp-thumb/{file_name}.jpg"
                if await aiopath.isfile(thumb_path):
                    thumb = thumb_path
                    LOGGER.info(f"Using yt-dlp thumbnail for: {file}")
                elif await aiopath.isfile(thumb_path.replace("/yt-dlp-thumb", "")):
                    thumb = thumb_path.replace("/yt-dlp-thumb", "")
                    LOGGER.info(f"Using adjacent thumbnail for: {file}")
                elif is_audio and not is_video:
                    thumb = await get_audio_thumbnail(self._up_path)
                    if thumb:
                        LOGGER.info(f"Using embedded audio thumbnail for: {file}")

            private_text = " ".join(
                filter(
                    None,
                    [
                        file,
                        getattr(self._listener, "source_url", ""),
                        getattr(self._listener, "file_details", {}).get("caption", ""),
                    ],
                )
            )
            self._private_dump_only = bool(self._private_dump_only or private_dump_only(private_text))
            f_size = await aiopath.getsize(self._up_path)
            route = await starfallx_upload.acquire_route(self._listener, f_size)
            self._active_route = route
            if getattr(route, "notice", ""):
                await send_message(self._listener.message, route.notice)

            if (
                self._listener.as_doc
                or force_document
                or (not is_video and not is_audio and not is_image)
            ):
                key = "documents"
                if is_video and thumb is None:
                    thumb = await get_video_thumbnail(self._up_path, None)
                    if thumb:
                        LOGGER.info(f"Using FFmpeg document thumbnail for: {file}")

                if self._listener.is_cancelled:
                    await starfallx_upload.release_route(route)
                    self._active_route = None
                    return
                if thumb == "none":
                    thumb = None
                if thumb:
                    doc_thumb = await get_telegram_document_thumb(thumb)
                send_thumb = doc_thumb or thumb
                if route.direct:
                    self._sent_msg = await self._send_direct_file(
                        route, key, cap_mono, thumb=send_thumb
                    )
                else:
                    self._sent_msg = await self._sent_msg.reply_document(
                        document=self._up_path,
                        quote=True,
                        thumb=send_thumb,
                        caption=cap_mono,
                        disable_content_type_detection=True,
                        disable_notification=True,
                        progress=self._upload_progress,
                    )
            elif is_video:
                key = "videos"
                duration = (await get_media_info(self._up_path))[0]
                if thumb is None and self._listener.thumbnail_layout:
                    thumb = await get_multiple_frames_thumbnail(
                        self._up_path,
                        self._listener.thumbnail_layout,
                        self._listener.screen_shots,
                    )
                    if thumb:
                        LOGGER.info(f"Using layout thumbnail for: {file}")
                if thumb is None:
                    thumb = await get_video_thumbnail(self._up_path, duration)
                    if thumb:
                        LOGGER.info(f"Using FFmpeg video thumbnail for: {file}")
                if thumb is not None and thumb != "none":
                    with Image.open(thumb) as img:
                        width, height = img.size
                else:
                    width = 480
                    height = 320
                if self._listener.is_cancelled:
                    await starfallx_upload.release_route(route)
                    self._active_route = None
                    return
                if thumb == "none":
                    thumb = None
                if route.direct:
                    self._sent_msg = await self._send_direct_file(
                        route,
                        key,
                        cap_mono,
                        thumb=thumb,
                        duration=duration,
                        width=width,
                        height=height,
                    )
                else:
                    self._sent_msg = await self._sent_msg.reply_video(
                        video=self._up_path,
                        quote=True,
                        caption=cap_mono,
                        duration=duration,
                        width=width,
                        height=height,
                        thumb=thumb,
                        cover=thumb,
                        supports_streaming=True,
                        disable_notification=True,
                        progress=self._upload_progress,
                    )
            elif is_audio:
                key = "audios"
                duration, artist, title = await get_media_info(self._up_path)
                if self._listener.is_cancelled:
                    await starfallx_upload.release_route(route)
                    self._active_route = None
                    return
                if thumb == "none":
                    thumb = None
                if route.direct:
                    self._sent_msg = await self._send_direct_file(
                        route,
                        key,
                        cap_mono,
                        thumb=thumb,
                        duration=duration,
                        artist=artist,
                        title=title,
                    )
                else:
                    self._sent_msg = await self._sent_msg.reply_audio(
                        audio=self._up_path,
                        quote=True,
                        caption=cap_mono,
                        duration=duration,
                        performer=artist,
                        title=title,
                        thumb=thumb,
                        disable_notification=True,
                        progress=self._upload_progress,
                    )
            else:
                key = "photos"
                if self._listener.is_cancelled:
                    await starfallx_upload.release_route(route)
                    self._active_route = None
                    return
                if route.direct:
                    self._sent_msg = await self._send_direct_file(route, key, cap_mono)
                else:
                    self._sent_msg = await self._sent_msg.reply_photo(
                        photo=self._up_path,
                        quote=True,
                        caption=cap_mono,
                        disable_notification=True,
                        progress=self._upload_progress,
                    )

            if (
                not self._listener.is_cancelled
                and self._media_group
                and not route.direct
                and (self._sent_msg.video or self._sent_msg.document)
            ):
                key = "documents" if self._sent_msg.document else "videos"
                if match := re_match(r".+(?=\.0*\d+$)|.+(?=\.part\d+\..+$)", o_path):
                    pname = match.group(0)
                    if pname in self._media_dict[key].keys():
                        self._media_dict[key][pname].append(
                            [self._sent_msg.chat.id, self._sent_msg.id]
                        )
                    else:
                        self._media_dict[key][pname] = [
                            [self._sent_msg.chat.id, self._sent_msg.id]
                        ]
                    msgs = self._media_dict[key][pname]
                    if len(msgs) == 10:
                        await self._send_media_group(pname, key, msgs)
                    else:
                        self._last_msg_in_group = True
                    queued_for_media_group = True

            if self._sent_msg and not queued_for_media_group and not getattr(route, "direct_final", False):
                self._queue_deferred_copy(self._sent_msg)

            if (
                self._thumb is None
                and thumb is not None
                and await aiopath.exists(thumb)
            ):
                await remove(thumb)
            if (
                doc_thumb
                and doc_thumb != thumb
                and await aiopath.exists(doc_thumb)
            ):
                await remove(doc_thumb)
            await starfallx_upload.release_route(route)
            self._active_route = None
        except (FloodWait, FloodPremiumWait) as f:
            LOGGER.warning(str(f))
            flood_wait = max(f.value + 1, f.value * get_tg_flood_wait_multiplier())
            await starfallx_upload.release_route(route, failed=bool(route and route.direct), flood_wait=flood_wait)
            self._active_route = None
            await sleep(flood_wait)
            if (
                self._thumb is None
                and thumb is not None
                and await aiopath.exists(thumb)
            ):
                await remove(thumb)
            if (
                doc_thumb
                and doc_thumb != thumb
                and await aiopath.exists(doc_thumb)
            ):
                await remove(doc_thumb)
            return await self._upload_file(cap_mono, file, o_path)
        except Exception as err:
            await starfallx_upload.release_route(route, failed=bool(route and route.direct))
            self._active_route = None
            if (
                self._thumb is None
                and thumb is not None
                and await aiopath.exists(thumb)
            ):
                await remove(thumb)
            if (
                doc_thumb
                and doc_thumb != thumb
                and await aiopath.exists(doc_thumb)
            ):
                await remove(doc_thumb)
            err_type = "RPCError: " if isinstance(err, RPCError) else ""
            LOGGER.error(f"{err_type}{err}. Path: {self._up_path}", exc_info=True)
            if isinstance(err, BadRequest) and key != "documents":
                LOGGER.error(f"Retrying As Document. Path: {self._up_path}")
                return await self._upload_file(cap_mono, file, o_path, True)
            raise err

    @property
    def speed(self):
        try:
            return self._processed_bytes / (time() - self._start_time)
        except ZeroDivisionError:
            return 0

    @property
    def processed_bytes(self):
        return self._processed_bytes

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Upload: {self._listener.name}")
        await self._listener.on_upload_error("your upload has been stopped!")
