from asyncio import gather
from dataclasses import dataclass, field
from json import loads
from os import path as ospath
from re import search
from urllib.parse import urljoin, urlparse

from httpx import AsyncClient, HTTPError, Response
from lxml import etree, html

from ... import LOGGER

HSTREAM_BASE = "https://hstream.moe"
HSTREAM_CATALOG = f"{HSTREAM_BASE}/v1/hentai-list"
HSTREAM_PLAYER_API = f"{HSTREAM_BASE}/player/api"
_HEADERS = {
    "Accept": "text/html,application/json,application/xhtml+xml",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}


class HstreamUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class HstreamCatalogItem:
    series_title: str
    episode: int
    slug: str

    @property
    def url(self):
        return f"{HSTREAM_BASE}/hentai/{self.slug}"


@dataclass(slots=True)
class HstreamStream:
    label: str
    resolution: str
    bit: str
    codec: str
    fps: float
    urls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class HstreamSubtitle:
    language: str
    url: str


@dataclass(slots=True)
class HstreamEpisode:
    title: str
    year: str
    description: str
    genres: list[str]
    views: int
    portrait_url: str
    landscape_url: str
    source_url: str
    streams: list[HstreamStream]
    subtitles: list[HstreamSubtitle]
    sample_urls: list[str]


def _image_url(value):
    if isinstance(value, dict):
        for key in ("url", "src", "poster", "image"):
            if result := _image_url(value.get(key)):
                return result
        return ""
    if isinstance(value, (list, tuple)):
        return next((result for item in value if (result := _image_url(item))), "")
    return str(value or "").strip()


def _absolute(url):
    value = _image_url(url)
    if not value or value.startswith(("data:", "blob:", "javascript:")):
        return ""
    absolute = urljoin(f"{HSTREAM_BASE}/", value)
    parsed = urlparse(absolute)
    return absolute if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _number(value):
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def _cookie_jar(path="cookies.txt"):
    cookies = {}
    if not ospath.isfile(path):
        return cookies
    try:
        with open(path, encoding="utf-8", errors="ignore") as cookie_file:
            for line in cookie_file:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7 and "hstream.moe" in parts[0]:
                    cookies[parts[5]] = parts[6]
    except OSError as error:
        LOGGER.warning(f"Unable to read Hstream cookies: {error}")
    return cookies


def _json_ld(document):
    for value in document.xpath(
        "//script[@type='application/ld+json']/text()"
    ):
        try:
            data = loads(value)
        except (TypeError, ValueError):
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if isinstance(entry, dict) and entry.get("@type") == "VideoObject":
                return entry
    return {}


def _interaction_count(value):
    entries = value if isinstance(value, list) else [value]
    for entry in entries:
        if isinstance(entry, dict) and entry.get("userInteractionCount") is not None:
            return _number(entry.get("userInteractionCount"))
    return 0


def _stream_domains(payload):
    domains = []
    values = payload.get("stream_domains") or payload.get("asia_stream_domains") or []
    if isinstance(values, str):
        values = [values]
    if isinstance(values, dict):
        values = list(values.values())
    for entry in values:
        if isinstance(entry, dict):
            entry = entry.get("url") or entry.get("domain")
        entry = str(entry or "").strip().rstrip("/")
        if entry and entry not in domains:
            domains.append(entry)
    return domains


def _codec_name(codec):
    value = str(codec or "").lower()
    if "av01" in value or value.startswith("av1"):
        return "AV1"
    if "hvc1" in value or "hev1" in value or "hevc" in value:
        return "x265"
    if "avc1" in value or "avc3" in value or "h264" in value:
        return "x264"
    if "vp09" in value or "vp9" in value:
        return "VP9"
    return "Unknown"


def _bit_depth(codec, attrs):
    for key in ("bitDepth", "bitsPerComponent", "maxBitDepth"):
        value = attrs.get(key)
        if value:
            return f"{value}bit"
    value = str(codec or "").lower()
    match = search(
        r"(?:av01|vp09)\.[^.]+\.[^.]+\.(0?8|10|12)(?:\.|$)", value
    )
    if match:
        return f"{int(match.group(1))}bit"
    return "8bit"


def _frame_rate(value):
    value = str(value or "0")
    if "/" in value:
        left, right = value.split("/", 1)
        try:
            return float(left) / float(right)
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_mpd(content, fallback_height, interpolated=False):
    try:
        root = etree.fromstring(content)
    except etree.XMLSyntaxError:
        return None
    videos = []
    for representation in root.xpath(
        "//*[local-name()='AdaptationSet'][contains(@mimeType, 'video')]"
        "/*[local-name()='Representation']"
        " | //*[local-name()='Representation'][contains(@mimeType, 'video')]"
    ):
        attrs = dict(representation.attrib)
        height = _number(attrs.get("height"))
        width = _number(attrs.get("width"))
        codec = attrs.get("codecs") or representation.getparent().get("codecs") or ""
        fps = _frame_rate(
            attrs.get("frameRate") or representation.getparent().get("frameRate")
        )
        videos.append((height, width, codec, fps, attrs))
    if not videos:
        return None
    height, _, codec, fps, attrs = max(videos, key=lambda row: (row[0], row[1]))
    height = height or fallback_height
    if interpolated and fps < 40:
        fps = 48.0
    return {
        "resolution": f"{height}p",
        "codec": _codec_name(codec),
        "bit": _bit_depth(codec, attrs),
        "fps": fps,
    }


class HstreamResolver:
    def __init__(self):
        self.client = AsyncClient(
            headers=_HEADERS,
            cookies=_cookie_jar(),
            follow_redirects=True,
            timeout=30,
        )
        # Several public Hstream CDN mirrors present certificates for their
        # parent domain. Keep site/API verification strict and relax it only
        # for the media mirrors the player itself advertises.
        self.stream_client = AsyncClient(
            headers=_HEADERS,
            cookies=_cookie_jar(),
            follow_redirects=True,
            timeout=30,
            verify=False,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.client.aclose()
        await self.stream_client.aclose()

    async def _request(self, method, url, **kwargs) -> Response:
        error = None
        for attempt in range(3):
            try:
                response = await self.client.request(method, url, **kwargs)
                if response.status_code < 500:
                    return response
                error = RuntimeError(f"HTTP {response.status_code}")
            except HTTPError as current:
                error = current
            if attempt < 2:
                from asyncio import sleep

                await sleep(2**attempt)
        raise RuntimeError(f"Hstream request failed for {url}: {error}")

    async def discover(self, letter):
        response = await self._request("GET", HSTREAM_CATALOG)
        response.raise_for_status()
        wanted = str(letter).casefold()
        items = {}
        for series in response.json():
            title = str(series.get("title") or "").strip()
            if not title.casefold().startswith(wanted):
                continue
            for episode in series.get("episodes") or []:
                slug = str(episode.get("slug") or "").strip()
                if not slug:
                    continue
                items[slug] = HstreamCatalogItem(
                    series_title=title,
                    episode=_number(episode.get("episode")),
                    slug=slug,
                )
        return sorted(
            items.values(),
            key=lambda item: (item.series_title.casefold(), item.episode, item.slug),
        )

    async def _probe_mpd(self, url, referer, height, interpolated):
        response = await self.stream_client.get(
            url, headers={"Referer": referer, "Accept": "application/dash+xml,*/*"}
        )
        if response.status_code != 200 or b"<MPD" not in response.content[:2000]:
            return None
        return _parse_mpd(response.content, height, interpolated)

    async def _probe_mp4(self, url, referer):
        response = await self.stream_client.get(
            url, headers={"Referer": referer, "Range": "bytes=0-1"}
        )
        return response.status_code in {200, 206}

    async def _streams(self, payload, referer):
        domains = _stream_domains(payload)
        stream_path = str(payload.get("stream_url") or "").strip("/")
        variants = [
            ("720p", "720", 720, False),
            ("1080p", "1080", 1080, False),
            ("2160p", "2160", 2160, False),
            ("1080p48fps", "1080i", 1080, True),
            ("2160p48fps", "2160i", 2160, True),
        ]

        resolved = []
        for label, directory, height, interpolated in variants:
            candidates = [
                f"{domain}/{stream_path}/{directory}/manifest.mpd"
                for domain in domains
            ]
            probes = await gather(
                *(
                    self._probe_mpd(url, referer, height, interpolated)
                    for url in candidates
                ),
                return_exceptions=True,
            )
            urls = []
            metadata = None
            for url, info in zip(candidates, probes, strict=True):
                if isinstance(info, Exception):
                    LOGGER.debug(f"Hstream MPD probe failed for {url}: {info}")
                    continue
                if info:
                    metadata = metadata or info
                    urls.append(url)
            if urls and metadata:
                resolved.append(
                    HstreamStream(label=label, urls=urls, **metadata)
                )

        if not any(stream.resolution == "720p" for stream in resolved):
            candidates = [
                f"{domain}/{stream_path}/x264.720p.mp4" for domain in domains
            ]
            probes = await gather(
                *(self._probe_mp4(url, referer) for url in candidates),
                return_exceptions=True,
            )
            legacy = []
            for url, available in zip(candidates, probes, strict=True):
                if isinstance(available, Exception):
                    LOGGER.debug(f"Hstream MP4 probe failed for {url}: {available}")
                elif available:
                    legacy.append(url)
            if legacy:
                resolved.append(
                    HstreamStream(
                        label="720p",
                        resolution="720p",
                        bit="8bit",
                        codec="x264",
                        fps=0,
                        urls=legacy,
                    )
                )
        return sorted(
            resolved,
            key=lambda stream: (
                _number(stream.resolution.rstrip("p")),
                1 if "48fps" in stream.label else 0,
            ),
        )

    async def resolve(self, item: HstreamCatalogItem):
        page = await self._request("GET", item.url)
        if page.status_code in {404, 410}:
            raise HstreamUnavailableError("stale or deleted Hstream catalog page")
        page.raise_for_status()
        document = html.fromstring(page.content)
        json_ld = _json_ld(document)
        episode_id = document.xpath("string(//input[@id='e_id']/@value)").strip()
        token = document.xpath("string(//input[@name='_token']/@value)").strip()
        if not episode_id:
            raise HstreamUnavailableError("stale or deleted Hstream catalog page")
        data = {"episode_id": episode_id}
        if token:
            data["_token"] = token
        player = await self._request(
            "POST",
            HSTREAM_PLAYER_API,
            data=data,
            headers={"Referer": item.url, "X-Requested-With": "XMLHttpRequest"},
        )
        if player.status_code in {404, 410}:
            raise HstreamUnavailableError("Hstream player data is unavailable")
        player.raise_for_status()
        payload = player.json()

        portrait = ""
        for attribute in ("data-src", "data-lazy-src", "src"):
            candidates = document.xpath(
                f"//img[contains(@{attribute}, 'cover-ep')]/@{attribute}"
            )
            portrait = next(
                (candidate for candidate in candidates if _absolute(candidate)),
                "",
            )
            if portrait:
                break
        landscape = json_ld.get("thumbnailUrl") or payload.get("poster") or ""
        upload_date = str(json_ld.get("uploadDate") or "")
        genres = json_ld.get("genre") or []
        if isinstance(genres, str):
            genres = [part.strip() for part in genres.split(",") if part.strip()]
        title = str(payload.get("title") or json_ld.get("name") or item.series_title).strip()
        streams = await self._streams(payload, item.url)
        subtitles = []
        seen_subtitles = set()
        for anchor in document.xpath("//a[@href]"):
            subtitle_url = _absolute(anchor.get("href"))
            if not subtitle_url.lower().split("?", 1)[0].endswith(
                (".ass", ".ssa", ".srt", ".vtt")
            ):
                continue
            if subtitle_url in seen_subtitles:
                continue
            label = " ".join(anchor.itertext()).strip().lower()
            download_name = str(anchor.get("download") or "").lower()
            language = "eng" if "english" in f"{label} {download_name}" else "und"
            subtitles.append(HstreamSubtitle(language=language, url=subtitle_url))
            seen_subtitles.add(subtitle_url)
        description_parts = document.xpath(
            "(//*[self::h1 or self::h2 or self::h3]"
            "[translate(normalize-space(.), 'DESCRIPTION', 'description')='description']"
            "/following::*[self::p or self::div][normalize-space()][1])//text()"
        )
        description = " ".join(
            " ".join(str(value).split())
            for value in description_parts
            if str(value).strip()
        ).strip()
        if not description:
            description = str(json_ld.get("description") or "").strip()

        sample_urls = []
        seen_samples = set()
        samples = document.xpath(
            "//img[contains(@data-te-img, 'gallery-ep-')]/@data-te-img"
        )
        if not samples:
            samples = document.xpath(
                "//a[contains(@href, 'gallery-ep-')]/@href"
                " | //img[contains(@data-src, 'gallery-ep-')]/@data-src"
                " | //img[contains(@src, 'gallery-ep-')]/@src"
            )
        for sample in samples:
            sample_url = _absolute(sample)
            if "gallery-ep-" not in sample_url or sample_url in seen_samples:
                continue
            sample_urls.append(sample_url)
            seen_samples.add(sample_url)
        return HstreamEpisode(
            title=title,
            year=upload_date[:4] if upload_date[:4].isdigit() else "",
            description=description,
            genres=[str(value).strip() for value in genres if str(value).strip()],
            views=_interaction_count(json_ld.get("interactionStatistic")),
            portrait_url=_absolute(portrait),
            landscape_url=_absolute(landscape),
            source_url=item.url,
            streams=streams,
            subtitles=subtitles,
            sample_urls=sample_urls,
        )
