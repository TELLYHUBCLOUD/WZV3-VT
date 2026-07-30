# Resolver logic adapted from SMDxTG/1TamilMV-Rss (AGPL-3.0).

from __future__ import annotations

from asyncio import to_thread
from dataclasses import dataclass
from hashlib import sha1
from os import path as ospath
from re import I, search, sub
from urllib.parse import unquote, urljoin, urlparse

from cloudscraper import create_scraper
from lxml import html


@dataclass(frozen=True)
class TamilMVCandidate:
    title: str
    topic_url: str
    torrent_url: str
    category: str
    size_bytes: int = 0


def _clean_title(value: str) -> str:
    value = unquote(" ".join((value or "").split()))
    value = sub(r"^\s*(?:www\.[^-\s]+[\s-]*)+", "", value, flags=I)
    value = sub(r"^\s*(?:\S*TamilMV\S*[\s-]*)+", "", value, flags=I)
    value = sub(r"\s*\.torrent\s*$", "", value, flags=I)
    return value.strip(" -") or "TamilMV release"


def _category(title: str) -> str:
    value = title.lower()
    if search(r"\b(?:s\d{1,2}|ep(?:isode)?\s*\d+|season|complete)\b", value, I):
        return "series"
    if any(token in value for token in ("dubbed", "multi audio", "tam+")):
        return "dubbed"
    return "movies"


def _size_bytes(text: str) -> int:
    match = search(r"(\d+(?:\.\d+)?)\s*(gb|mb|kb)", text or "", I)
    if not match:
        return 0
    scale = {"kb": 1024, "mb": 1024**2, "gb": 1024**3}[match.group(2).lower()]
    return int(float(match.group(1)) * scale)


def _topic_links(root, base_url: str) -> list[str]:
    result = []
    for href in root.xpath("//a[contains(@href, 'topic')]/@href"):
        url = urljoin(base_url, href)
        if url not in result:
            result.append(url)
    return result


def _torrent_candidates(root, topic_url: str) -> list[TamilMVCandidate]:
    candidates = []
    seen = set()
    anchors = root.xpath(
        "//div[contains(@class, 'cPost_contentWrap')]//a[@href] | //a[@href]"
    )
    for anchor in anchors:
        label = " ".join(anchor.text_content().split())
        href = urljoin(topic_url, anchor.get("href", ""))
        hint = f"{label} {href}".lower()
        if "torrent" not in hint or href in seen:
            continue
        seen.add(href)
        parent_text = " ".join(anchor.getparent().text_content().split()) if anchor.getparent() is not None else label
        title = _clean_title(label or ospath.basename(urlparse(href).path))
        candidates.append(
            TamilMVCandidate(
                title=title,
                topic_url=topic_url,
                torrent_url=href,
                category=_category(title),
                size_bytes=_size_bytes(parent_text),
            )
        )
    return candidates


def _resolve_sync(url: str, max_topics: int = 40) -> list[TamilMVCandidate]:
    scraper = create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True}
    )
    response = scraper.get(url, timeout=35)
    response.raise_for_status()
    root = html.fromstring(response.content)
    direct = _torrent_candidates(root, response.url)
    if direct:
        return direct
    found = []
    seen = set()
    for topic_url in _topic_links(root, response.url)[:max_topics]:
        try:
            topic = scraper.get(topic_url, timeout=35)
            topic.raise_for_status()
            topic_root = html.fromstring(topic.content)
            for item in _torrent_candidates(topic_root, topic.url):
                if item.torrent_url not in seen:
                    seen.add(item.torrent_url)
                    found.append(item)
        except Exception:
            continue
    return found


async def resolve_tamilmv(url: str, max_topics: int = 40) -> list[TamilMVCandidate]:
    return await to_thread(_resolve_sync, url, max_topics)


def _torrent_infohash(payload: bytes) -> str:
    def skip_value(position: int) -> int:
        token = payload[position : position + 1]
        if token == b"i":
            end = payload.index(b"e", position + 1)
            return end + 1
        if token in {b"l", b"d"}:
            position += 1
            while payload[position : position + 1] != b"e":
                position = skip_value(position)
            return position + 1
        colon = payload.index(b":", position)
        length = int(payload[position:colon])
        return colon + 1 + length

    try:
        if payload[:1] != b"d":
            raise ValueError("torrent root is not a dictionary")
        position = 1
        while payload[position : position + 1] != b"e":
            colon = payload.index(b":", position)
            key_length = int(payload[position:colon])
            key_start = colon + 1
            key_end = key_start + key_length
            key = payload[key_start:key_end]
            value_start = key_end
            value_end = skip_value(value_start)
            if key == b"info":
                return sha1(payload[value_start:value_end]).hexdigest()
            position = value_end
    except (IndexError, ValueError):
        pass
    return sha1(payload).hexdigest()


def _download_sync(url: str, target: str) -> str:
    scraper = create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True}
    )
    response = scraper.get(url, timeout=60)
    response.raise_for_status()
    payload = response.content
    if not payload or b"4:info" not in payload[:4096]:
        raise ValueError("TamilMV did not return a valid torrent file")
    with open(target, "wb") as stream:
        stream.write(payload)
    return _torrent_infohash(payload)


async def download_torrent(url: str, target: str) -> str:
    return await to_thread(_download_sync, url, target)
