#!/usr/bin/env bash
set -Eeuo pipefail

log() {
    printf '[start] %s\n' "$*"
}

repair_packages() {
    if [ "$(id -u)" -ne 0 ]; then
        log "Required binaries are missing and the container is not running as root; cannot auto-repair packages."
        return 1
    fi

    log "Attempting package repair with apt-get."
    apt-get update
    apt-get install -y --no-install-recommends \
        aria2 ffmpeg imagemagick mediainfo mktorrent qbittorrent-nox rclone sabnzbdplus
    rm -rf /var/lib/apt/lists/*
}

require_binary() {
    local name="$1"
    local package="${2:-$1}"
    if command -v "$name" >/dev/null 2>&1; then
        log "Found $name at $(command -v "$name")"
        return 0
    fi

    log "Missing required binary: $name (package: $package)"
    repair_packages
    if ! command -v "$name" >/dev/null 2>&1; then
        log "Package repair did not provide $name. Startup cannot continue."
        return 1
    fi
    log "Repaired $name at $(command -v "$name")"
}

main() {
    require_binary aria2c aria2
    require_binary qbittorrent-nox qbittorrent-nox
    require_binary ffmpeg ffmpeg
    require_binary rclone rclone
    require_binary mktorrent mktorrent
    require_binary mediainfo mediainfo
    require_binary montage imagemagick

    if [ "${DISABLE_NZB:-false}" != "true" ] && [ "${DISABLE_NZB:-False}" != "True" ]; then
        require_binary sabnzbdplus sabnzbdplus
    fi

    mkdir -p downloads download-temp torrents/seeding thumbnails rclone logs

    log "Running repository update hook."
    python update.py

    log "Starting bot."
    exec python -m bot
}

main "$@"
