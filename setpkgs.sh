#!/usr/bin/env bash
set -Eeuo pipefail

ARIA2C=${1:-aria2c}
SERVICE_CORES=${2:-}
CPU_LIMIT=${3:-20}
SABNZBDPLUS=${4:-}

log() {
    printf '[services] %s\n' "$*"
}

fail() {
    log "ERROR: $*"
    exit 1
}

require_binary() {
    command -v "$1" >/dev/null 2>&1 || fail "Required binary '$1' is not installed or not in PATH."
}

run_with_affinity() {
    if [ -n "$SERVICE_CORES" ]; then
        taskset -c "$SERVICE_CORES" "$@"
    else
        "$@"
    fi
}

wait_for_tcp() {
    local host="$1"
    local port="$2"
    local name="$3"
    local attempt
    for attempt in $(seq 1 30); do
        if nc -z "$host" "$port" >/dev/null 2>&1; then
            log "$name is listening on $host:$port."
            return 0
        fi
        sleep 1
    done
    fail "$name did not listen on $host:$port within 30 seconds."
}

wait_for_aria2_rpc() {
    local attempt
    for attempt in $(seq 1 30); do
        if curl -fsS \
            -H 'Content-Type: application/json' \
            --data '{"jsonrpc":"2.0","id":"wzml-health","method":"aria2.getVersion"}' \
            http://127.0.0.1:6800/jsonrpc >/dev/null 2>&1; then
            log "aria2 RPC is ready."
            return 0
        fi
        sleep 1
    done
    fail "aria2 RPC did not become ready on http://127.0.0.1:6800/jsonrpc."
}

mkdir -p downloads download-temp logs configs/ac configs/sabnzbd

require_binary "$ARIA2C"
require_binary curl
require_binary nc

if pgrep -x "$ARIA2C" >/dev/null 2>&1; then
    log "Stopping previous aria2 process before restart."
    pkill -x "$ARIA2C" || true
fi

tracker_list=""
if tracker_list=$(curl -fsSL --max-time 20 https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_all.txt | awk 'NF' | paste -sd, -); then
    log "Loaded public tracker list for aria2."
else
    log "Tracker list download failed; starting aria2 without extra trackers."
    tracker_list=""
fi

aria2_args=(
    --conf-path=configs/ac/ac.conf
    --daemon=true
    --rpc-listen-all=true
    --rpc-listen-port=6800
    --dir=/usr/src/app/downloads
    --input-file=/usr/src/app/downloads/aria2.session
    --save-session=/usr/src/app/downloads/aria2.session
    --log=/usr/src/app/logs/aria2.log
    --log-level=notice
)

if [ -n "$tracker_list" ]; then
    aria2_args+=(--bt-tracker="$tracker_list")
fi

touch /usr/src/app/downloads/aria2.session
log "Starting aria2."
run_with_affinity "$ARIA2C" "${aria2_args[@]}"
wait_for_tcp 127.0.0.1 6800 aria2
wait_for_aria2_rpc

if [ -n "$SABNZBDPLUS" ]; then
    require_binary "$SABNZBDPLUS"
    require_binary cpulimit

    if pgrep -f "$SABNZBDPLUS" >/dev/null 2>&1; then
        log "Stopping previous SABnzbd process before restart."
        pkill -f "$SABNZBDPLUS" || true
    fi

    log "Starting SABnzbd."
    if [ -n "$SERVICE_CORES" ]; then
        taskset -c "$SERVICE_CORES" cpulimit -l "$CPU_LIMIT" -- "$SABNZBDPLUS" -f configs/sabnzbd/SABnzbd.ini -s 0.0.0.0:8070 -b 0 -d -c -l 0 --console
    else
        cpulimit -l "$CPU_LIMIT" -- "$SABNZBDPLUS" -f configs/sabnzbd/SABnzbd.ini -s 0.0.0.0:8070 -b 0 -d -c -l 0 --console
    fi
    wait_for_tcp 127.0.0.1 8070 SABnzbd
fi
