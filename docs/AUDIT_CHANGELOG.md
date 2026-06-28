# Deployment Audit Changelog

## Bugs Found

- The Dockerfile inherited from `mysterysd/wzmlx:v3`, so the repository depended on binaries and Python helpers that were not part of the repo.
- `bot/core/config_manager.py` imported `wz_bin`, which was supplied by the old image and was missing from a fresh checkout.
- Service binaries were derived from custom aliases instead of standard package names.
- `start.sh` launched Python directly without verifying required binaries or preparing runtime directories.
- `setpkgs.sh` started aria2 and SABnzbd without validating binary availability, port readiness, or aria2 JSON-RPC readiness.
- qBittorrent startup slept for a fixed two seconds and then assumed the Web API was available.
- `bot/core/startup.py` built a service bootstrap command with shell string interpolation.
- The web server startup used a shell command with an inline environment assignment.
- `docker-compose.yml` included stale comments, did not expose the app port directly, lacked a healthcheck, and did not persist logs/rclone/thumbnails.
- README deployment commands used `docker buildx compose`, which is not the normal Compose command.
- `requirements.txt` included the obsolete PyPI `asyncio` backport even though Python 3.12 provides `asyncio` in the standard library.

## Fixes Made

- Replaced the inherited image with `python:3.12-slim-bookworm`.
- Installed all required OS packages in Docker: aria2, qBittorrent, ffmpeg, rclone, SABnzbd, mktorrent, ImageMagick, mediainfo, p7zip, and runtime helpers.
- Created `/wzvenv` in the image and installed Python dependencies with `uv`.
- Removed the `wz_bin` dependency and mapped binary names to standard package binaries.
- Rebuilt `start.sh` with strict error handling, binary checks, package repair, runtime directory creation, and clear logs.
- Rebuilt `setpkgs.sh` with strict error handling, tracker fallback, aria2 session persistence, daemon logs, TCP checks, and aria2 RPC checks.
- Changed startup service bootstrap to `create_subprocess_exec` argument lists.
- Changed gunicorn startup to `create_subprocess_exec` with an explicit environment dictionary.
- Added qBittorrent Web API readiness polling before creating the qBittorrent client.
- Simplified Compose to the app and tunnel services, added persistent mounts, direct `8080` publishing, `init: true`, restart policy, stop grace period, and a healthcheck.
- Corrected README commands and documented fresh VPS deployment in `docs/DEPLOYMENT.md`.
- Removed the obsolete `asyncio` dependency from `requirements.txt`.

## Remaining Issues

- A real Docker build and runtime smoke test could not be performed in the current Windows workspace because Docker is not installed on PATH here.
- Shell syntax validation could not be run locally because `bash` is not installed on PATH here.
- Full functional testing still requires real Telegram credentials, MongoDB, and any optional service credentials.
