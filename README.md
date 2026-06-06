<p align="center">
  <img src="docs/WZML-X.png" alt="WZML-X logo" width="420">
</p>

<h1 align="center">StarFallX WZML-X v1.2</h1>

<p align="center">
  A StarFallX-flavoured WZML-X build focused on Telegram leeching, Video Tools, anime/media thumbnails, AutoRename, Auto Process, helper-token uploads, and safer FFmpeg handling.
</p>

> Special thanks to aquib and the WZML-X/mirror-leech community.  
> This is a dropout/experimental feature project: if any feature is useful, deploy it, copy it, improve it, or develop it further.

## Index

- [What This Build Adds](#what-this-build-adds)
- [Quick Deploy](#quick-deploy)
- [Required Config](#required-config)
- [Video Tools](#video-tools)
- [Auto Process](#auto-process)
- [AutoRename](#autorename)
- [Auto Thumbnail](#auto-thumbnail)
- [Intro Subtitles](#intro-subtitles)
- [Batch Leech](#batch-leech)
- [Create Torrent](#create-torrent)
- [StarFallX Upload Engine](#starfallx-upload-engine)
- [Settings Backup](#settings-backup)
- [Restart Recovery](#restart-recovery)
- [CPU And Performance](#cpu-and-performance)
- [Testing And Support](#testing-and-support)
- [Known Limits](#known-limits)
- [Credits](#credits)

## What This Build Adds

This repo keeps the WZML-X mirror/leech base and adds a media-focused StarFallX layer:

| Area | Features |
|---|---|
| Video Tools | Manual `-vt`, stream remove/extract, track order, merge tracks, subtitle translate, video+video merge |
| Auto Process | Unzip, order tracks, remove streams, smart merge, intro subtitles, metadata, AutoRename, sequential upload |
| Thumbnail | TMDb -> AniList -> MyAnimeList -> local FFmpeg frame fallback, HD provider images, auto video cover support |
| AutoRename | Reverse template creator, dotted/underscored filename parsing, DS4K variable, audio codec/channel tags |
| Upload | StarFallX helper-token routing, user helper tokens, global helper pool, premium user-session support |
| Safety | FFmpeg queue, archive queue, CPU/RAM guards, quieter process messages |
| Batch | `/bleech` for many links and `/bqleech` for huge qB torrents in ordered batches |
| Release pack | `/ctorrent` thumbnail, contact sheet, BBCode description, torrent file |

## Quick Deploy

Use Docker Compose on a Linux VPS. This build is tested mainly on Linux. Heroku container files and ARM/buildx-friendly Docker files are present, but ARM and Heroku are not fully tested.

```bash
git clone https://github.com/YOUR_USERNAME/WZML-X.git
cd WZML-X
cp config_sample.py config.py
nano config.py
docker compose build --no-cache
docker compose up -d
docker compose logs -f
```

If the bot says `BOT_TOKEN variable is missing`, confirm `config.py` exists in the repo root and Docker Compose mounts it into `/usr/src/app/config.py`.

```bash
ls -l config.py
grep -n "config.py" docker-compose.yml
```

## Required Config

Minimum values:

```python
BOT_TOKEN = ""
TELEGRAM_API = 0
TELEGRAM_HASH = ""
OWNER_ID = 0
DATABASE_URL = ""
```

Recommended media values:

```python
USER_SESSION_STRING = ""
LEECH_DUMP_CHAT = ""
TMDB_ACCESS_TOKEN = ""
AUTO_THUMBNAIL = True
AUTORENAME = True
SEQUENTIAL_LEECH = True
```

For anime fallback thumbnails, MyAnimeList is optional:

```python
MYANIMELIST_CLIENT_ID = ""
MYANIMELIST_CLIENT_NAME = ""
```

## Video Tools

Manual Video Tools are opened with `-vt`:

```text
/l <link> -vt
/l <link> -vt -e
/l <link> -vt -i 12
```

When `-vt` is used, the bot waits for the download/extract to finish if streams are not available yet, then opens the Video Tools UI.

Supported actions:

- Remove Stream
- Extract Stream
- Change Order
- Audio Order
- Subtitle Order
- Merge Tracks
- Translate Subs
- Video + Video

Extract Stream is exclusive. If you choose Extract Stream, the bot uploads only the extracted audio/subtitle files and does not apply video metadata, intro subtitle, or AutoRename to the extracted folder.

Audio/subtitle order accepts short language names:

```text
tam tel eng
```

If keep mode is active, unmatched streams are removed. If order mode is active without keep mode, matched streams are moved first and safe remaining streams are preserved.

Video + Video merge:

```text
/l <first-link> -vt -i 12
```

The bot asks for total video files, downloads them in order, sends a planner, and merges with FFmpeg concat when the inputs are compatible.

## Auto Process

Auto Process flow:

```text
download -> unzip -> order tracks -> remove streams -> smart merge -> intro subtitle -> metadata -> auto rename -> sequential upload
```

Useful settings:

- Auto Process
- Auto Leech
- Auto Unzip
- Auto Remove Streams
- Keep Audios
- Keep Subtitles
- Audios Order
- Subtitles Order
- Auto Merge
- Intro Subtitle
- Metadata
- AutoRename

Rules:

- Auto Remove conflicts with keep/order values. Enable only one style.
- Keep Audios/Subtitles accepts values such as `tam eng`.
- Audios Order/Subtitles Order accepts ordered values such as `tam tel eng`.
- Auto Process sends only planners, warnings, and finish messages. Detailed speed/progress stays in `/status`.

## AutoRename

AutoRename supports direct templates and reverse-template creation.

Example source name:

```text
[S01E08] Off Campus (2026) 1080p 10bit AMZN WEBRip x265 [Tamil-DDP 5.1] ESub ~ PSA
```

Example generated template:

```text
[S{season}E{episode}] {name} {year} {resolution} {bit} {ott} {quality} {codec} [{languages}-{audio_codec} {audio_channels}] {shortsub} ~ {release_group}
```

Task override:

```text
/l <link> -ar custom [S{season}E{episode}] {name} {resolution} {DS4K} {codec}
```

Common variables:

```text
{file_name} {file_size} {file_caption} {languages} {subtitles} {duration}
{ott} {resolution} {name} {title} {year} {quality} {DS4K}
{season} {episode} {audio} {lib} {extension} {shortsub} {shortlang}
{part} {raw_name} {link} {vcodec} {codec} {acodec}
{audio_codec} {audio_channels} {audio_bitrate}
{hdr} {dynamic_range} {release_group} {group}
```

Notes:

- `{DS4K}` is separate.
- `{resolution}` stays only values like `2160p`, `1080p`, `720p`.
- `{quality}` stays values like `WEB-DL`, `WEBRip`, `BluRay`.
- Missing variables render blank instead of leaving raw placeholders.

## Auto Thumbnail

Thumbnail provider flow:

```text
TMDb -> AniList -> MyAnimeList -> local FFmpeg frame
```

Custom user thumbnails always win. Provider thumbnails are saved from HD sources. Document thumbnails are generated from the HD source only when Telegram needs a smaller document thumb.

If a thumbnail looks wrong, check:

- `TMDB_ACCESS_TOKEN`
- title cleanup in logs: `Poster search title`
- AniList/MAL availability
- whether local FFmpeg fallback was used

## Intro Subtitles

Intro subtitles are generated as ASS subtitles and muxed into the video, not burned into the pixels.

Useful config:

```python
INTRO_SUBTITLE_TEXT = ""
INTRO_SUBTITLE_RANGES = "00:00:00 - 00:00:05 (5s)"
INTRO_SUBTITLE_FADE_MS = 400
INTRO_SUBTITLE_FONT = "Arial"
INTRO_SUBTITLE_FONT_SIZE = 36
INTRO_SUBTITLE_COLOR = "&H00FFFFFF"
INTRO_SUBTITLE_OUTLINE_COLOR = "&H00000000"
INTRO_SUBTITLE_COLOR_PALETTE = ""
```

`INTRO_SUBTITLE_COLOR_PALETTE` can cycle letter colors when set, for example:

```python
INTRO_SUBTITLE_COLOR_PALETTE = "#ff4aa2,#4ad8ff,#fff176"
```

## Batch Leech

`/bleech` leeches many links with controlled download/upload limits:

```text
/bleech link1 link2 link3
```

Config:

```python
BLEECH_MAX_ACTIVE_DOWNLOADS = 1
BLEECH_MAX_ACTIVE_UPLOADS = 2
BLEECH_LINK_SIZE_LIMIT_GB = 0
```

`/bqleech`, `/bql`, and `/bqbleech` handle very large qB torrents by selecting ordered batches:

```text
/bqleech <magnet-or-torrent>
```

Config:

```python
BQLEECH_BATCH_SIZE_GB = 30
BQLEECH_MAX_ACTIVE_DOWNLOADS = 1
BQLEECH_MAX_ACTIVE_UPLOADS = 1
```

## Create Torrent

`/ctorrent` creates a release pack:

```text
/ctorrent <link>
/ctorrent 'Folder Name' - <link1> <link2> <link3>
```

Single-file mode can send:

- HD thumbnail JPEG
- 5x3 contact sheet with file info
- `description.txt` BBCode template
- `.torrent`

Folder mode stores files under:

```text
/usr/src/app/torrents/seeding/Folder Name
```

Contact sheets are saved under:

```text
/usr/src/app/torrents/seeding/Folder Name/Screenshots
```

## StarFallX Upload Engine

The upload engine is designed for stable multi-file uploads, not magic one-file speed.

Rules:

- One helper bot token runs one active upload at a time.
- Normal users use their own helper token list.
- Owner/sudo tasks may use the approved helper pool when configured.
- If helper tokens are busy/cooling, files queue or fall back according to config.
- 2GB+ uploads need premium user-session support or normal split behavior.
- Helper bots should be added to `LEECH_DUMP_CHAT` for sequential dump/copy support.

Important config:

```python
UPLOAD_ENGINE = "StarFallX"
UPLOAD_ENGINE_VERSION = "1.2"
USER_BOT_TOKEN_UPLOAD = True
HELPER_TOKEN_BACKUP_LIMIT = 5
GLOBAL_UPLOAD_BOT_ENABLED = True
MAIN_BOT_FALLBACK_UPLOADS = 1
UPLOAD_SAFE_CPU_GUARD = True
UPLOAD_BOT_COOLDOWN_SECONDS = 300
```

## Settings Backup

User Settings has zip export/import support. It imports safe user settings only.

Protected values are skipped:

- helper raw tokens
- PIN hashes
- session strings
- cookies
- passwords
- raw API keys
- masked token values

Users must re-add helper tokens manually after importing a backup.

## Restart Recovery

This build supports restart recovery for batch controllers:

```python
BATCH_TASK_RESTART_RESUME = True
```

Supported:

- `/bleech`: resumes from the next pending link.
- `/bqleech` / `/bqbleech`: reloads the saved source and re-plans from the next pending batch when possible.

Not fully supported:

- ordinary single `/leech` and `/mirror` tasks do not safely auto-restart after reboot yet.

Reason: normal tasks need a deeper command/upload-state journal to avoid duplicated Telegram posts or restarting half-finished uploads. Existing incomplete-task notifier can report links after restart, but it is not the same as safe automatic replay.

## CPU And Performance

Safe defaults are better than unlimited speed.

Important knobs:

```python
PERFORMANCE_PROFILE = "auto"
FFMPEG_THREADS = 0
FFMPEG_CPU_CORES = ""
FFMPEG_QUEUE_ENABLED = True
FFMPEG_QUEUE_LOGS = True
SAFE_CPU_PERCENT = 92
SAFE_FREE_RAM_MB = 512
MAX_PARALLEL_TASKS = 4
TG_COPY_DELAY = 0.15
```

FFmpeg queue means one FFmpeg operation runs at a time by default. This protects small VPS machines from crashes while upload/download tasks continue normally.

For low-CPU VPS:

```python
PERFORMANCE_PROFILE = "safe"
FFMPEG_THREADS = 2
MAX_PARALLEL_TASKS = 2
```

For stronger VPS:

```python
PERFORMANCE_PROFILE = "max_speed"
FFMPEG_THREADS = 0
MAX_PARALLEL_TASKS = 4
```

Always check `/status`, CPU, RAM, and free disk while testing.

## Testing And Support

Testing group:

```text
@Anime_Starfall
```

Use Linux logs when reporting bugs. ARM and Heroku container support exists, but if it fails, collect logs and ask AI/devs to adjust the Docker/package layer.

Useful commands:

```bash
docker compose logs -f
docker compose ps
docker compose build --no-cache
python -m compileall bot
```

## Known Limits

- Normal single-task restart replay is not fully safe yet.
- Multiple bot tokens improve parallel multi-file uploads, not one-file upload speed.
- Telegram FloodWait still applies to all bot/user sessions.
- Some Video + Video merges require compatible codecs/timebases; incompatible files should be remuxed manually or processed later.
- Heroku and ARM support are available in files but not fully tested by this release.
- Auto thumbnail depends on metadata provider availability and title cleanup.

## Credits

Special thanks to aquib for WZML-X work and for accepting community feature ideas.

WZML-X is based on the mirror-leech ecosystem and the work of many upstream contributors, including the original mirror-leech-telegram-bot project.

This StarFallX branch is a community feature/dropout project. Take what helps, improve it, and keep the logs clean.
