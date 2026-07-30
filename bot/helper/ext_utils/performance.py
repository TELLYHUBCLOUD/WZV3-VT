from os import cpu_count

from psutil import cpu_percent, virtual_memory

from ...core.config_manager import Config


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_performance_profile():
    profile = str(getattr(Config, "PERFORMANCE_PROFILE", "max_speed") or "max_speed")
    profile = profile.strip().lower()
    if profile != "auto":
        return profile
    cpu_total = max(1, cpu_count() or 1)
    ram_mb = virtual_memory().total // (1024 * 1024)
    if cpu_total <= 2 or ram_mb <= 4096:
        return "safe"
    if cpu_total <= 4 or ram_mb <= 6144:
        return "balanced"
    return "max_speed"


def get_ffmpeg_threads():
    cpu_total = max(1, cpu_count() or 1)
    configured = _safe_int(getattr(Config, "FFMPEG_THREADS", 0))
    if configured > 0:
        return max(1, min(configured, cpu_total))

    profile = get_performance_profile()
    if profile == "safe":
        return 1
    if profile == "balanced":
        return max(1, min(2, cpu_total // 2 or 1))
    return max(1, min(3, cpu_total // 2 or 1))


def get_ffmpeg_cores():
    configured = str(getattr(Config, "FFMPEG_CPU_CORES", "") or "").strip()
    if configured:
        return configured
    return ",".join(str(i) for i in range(get_ffmpeg_threads()))


def get_tg_copy_delay():
    delay = _safe_float(getattr(Config, "TG_COPY_DELAY", 0.15), 0.15)
    return max(0.0, min(delay, 5.0))


def get_tg_flood_wait_multiplier():
    multiplier = _safe_float(getattr(Config, "TG_FLOOD_WAIT_MULTIPLIER", 1.1), 1.1)
    return max(1.0, min(multiplier, 3.0))


def get_max_parallel_tasks():
    configured = _safe_int(getattr(Config, "MAX_PARALLEL_TASKS", 0))
    if configured > 0:
        profile_cap = {"safe": 2, "balanced": 3}.get(get_performance_profile())
        return min(configured, profile_cap) if profile_cap else configured

    profile = get_performance_profile()
    if profile == "safe":
        return 2
    if profile == "balanced":
        return 3
    return 4


def get_premium_upload_workers():
    configured = max(
        1,
        min(3, _safe_int(getattr(Config, "PREMIUM_UPLOAD_WORKERS", 2), 2)),
    )
    profile = get_performance_profile()
    if profile == "safe":
        return 1
    if profile == "balanced":
        return min(configured, 2)
    return configured


def get_safe_cpu_percent():
    return max(0, min(_safe_int(getattr(Config, "SAFE_CPU_PERCENT", 92), 92), 100))


def get_safe_free_ram_mb():
    return max(0, _safe_int(getattr(Config, "SAFE_FREE_RAM_MB", 512), 512))


def resources_overloaded():
    cpu_limit = get_safe_cpu_percent()
    ram_limit_mb = get_safe_free_ram_mb()
    reasons = []

    if cpu_limit:
        used_cpu = cpu_percent(interval=0.1)
        if used_cpu >= cpu_limit:
            reasons.append(f"CPU {used_cpu:.1f}% >= {cpu_limit}%")

    if ram_limit_mb:
        free_ram_mb = virtual_memory().available // (1024 * 1024)
        if free_ram_mb <= ram_limit_mb:
            reasons.append(f"free RAM {free_ram_mb}MB <= {ram_limit_mb}MB")

    return bool(reasons), ", ".join(reasons)
