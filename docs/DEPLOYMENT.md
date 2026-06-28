# Fresh VPS Docker Deployment

This repository is designed to build from a clean Docker host. The app image no longer depends on `mysterysd/wzmlx:v3` or any binaries from an older VPS.

## Fresh Ubuntu VPS

1. Install Docker Engine and the Compose plugin from Docker's official Ubuntu instructions.
2. Clone or upload this repository.
3. Create the runtime files and directories:

```bash
cp config_sample.py config.py
mkdir -p accounts downloads logs rclone thumbnails
```

4. Edit `config.py` and set at least:

```text
BOT_TOKEN
TELEGRAM_API
TELEGRAM_HASH
OWNER_ID
DATABASE_URL
```

5. Build and start:

```bash
docker compose up -d --build
```

6. Watch startup:

```bash
docker compose logs -f app
docker compose logs -f tunnel
```

The app listens on host port `8080`. The bundled quick tunnel also writes the active Cloudflare URL to the tunnel logs and `/data/tunnel_url.txt` inside the shared tunnel volume.

## What Docker Installs

The main image installs the required operating-system tools during build:

- `aria2`
- `qbittorrent-nox`
- `ffmpeg`
- `rclone`
- `sabnzbdplus`
- `mktorrent`
- `imagemagick`
- `mediainfo`
- `p7zip-full`
- runtime helpers such as `curl`, `cpulimit`, `tini`, `procps`, and `netcat-openbsd`

Python dependencies are installed into `/wzvenv` with `uv` from `requirements.txt`.

## Startup Checks

`start.sh` verifies the important binaries before launching the bot. If a binary is missing and the container is running as root, it attempts an `apt-get` repair and fails loudly if the binary is still unavailable.

`setpkgs.sh` starts aria2 and SABnzbd, then waits for:

- aria2 TCP port `6800`
- aria2 JSON-RPC readiness
- SABnzbd TCP port `8070` when NZB support is enabled

The Python torrent manager starts `qbittorrent-nox` and waits for the Web API on `8090` before creating the client.

## Common Commands

```bash
docker compose ps
docker compose logs -f app
docker compose restart app
docker compose down
docker compose up -d --build
```

## Troubleshooting

- Missing `config.py`: create it from `config_sample.py` and fill the required values.
- aria2 RPC errors: check `docker compose logs app` and `logs/aria2.log`.
- qBittorrent startup errors: confirm `configs/qbittorrent/qBittorrent/config/qBittorrent.conf` is present and the app logs show `qBittorrent Web API` readiness.
- SABnzbd errors: enable NZB only after setting `USENET_SERVERS` and confirm `configs/sabnzbd/SABnzbd.ini` is present.
- Port conflict: change the host side of `8080:8080` in `docker-compose.yml`.
