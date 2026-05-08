#!/usr/bin/env python3
"""Scrape selected Labs website pages from the sitemap into Markdown files."""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from typing import Iterable, cast
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag
from bs4.element import ProcessingInstruction
from markdownify import markdownify as html_to_markdown


# -----------------------------
# Config
# -----------------------------

BASE_DIR = Path(__file__).resolve().parent
SITEMAP_URL = "https://lab.neos.eu/sitemap.xml"

TARGET_PREFIXES = [
    "https://lab.neos.eu/thinktank/glossar",
    "https://lab.neos.eu/thinktank/publikationen",
]

ALLOWED_ASSET_ORIGINS = [
    "https://lab.neos.eu",
    "https://www.neos.eu",
]

OUTPUT_DIR = BASE_DIR / "LabsWebsiteContent"
MEDIA_DIR_NAME = "Media"

SELECTORS = {
    "content": "main#main",
    "links": "a[href]",
    "assets": [
        "a[href]",
        "img[src]",
        "img[data-src]",
        "source[src]",
        "video[src]",
        "audio[src]",
        "track[src]",
        "iframe[src]",
        "embed[src]",
        "object[data]",
    ],
}

ASSET_EXTENSIONS = {
    ".apng",
    ".avif",
    ".bmp",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".json",
    ".mp3",
    ".mp4",
    ".odp",
    ".ods",
    ".odt",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".svg",
    ".webm",
    ".webp",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}

REQUEST_DELAY_SECONDS = 2.1
REQUEST_TIMEOUT_SECONDS = 30
USER_AGENT = "LabsWebsiteContentScapper/1.0 (+https://lab.neos.eu)"


class RateLimitedClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def get(self, url: str) -> requests.Response:
        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response
        finally:
            time.sleep(REQUEST_DELAY_SECONDS)


def main() -> None:
    args = parse_args()
    client = RateLimitedClient()
    output_dir = args.output_dir.expanduser().resolve()
    asset_cache: dict[str, Path] = {}

    recreate_output_dir(output_dir)

    sitemap_urls = load_sitemap_urls(client, SITEMAP_URL)
    queue = deque(sorted({normalize_page_url(url) for url in sitemap_urls if is_target_page(url)}))
    queue.extend(normalize_page_url(url) for url in TARGET_PREFIXES)

    queued_pages = set(queue)
    scraped_pages: set[str] = set()
    failed_pages: list[tuple[str, str]] = []

    while queue:
        page_url = queue.popleft()
        if page_url in scraped_pages:
            continue

        try:
            discovered = scrape_page(client, page_url, output_dir, asset_cache)
            scraped_pages.add(page_url)
        except Exception as error:  # noqa: BLE001 - keep crawling after a single bad page.
            failed_pages.append((page_url, str(error)))
            continue

        for discovered_url in discovered:
            normalized_url = normalize_page_url(discovered_url)
            if normalized_url not in queued_pages and normalized_url not in scraped_pages:
                queued_pages.add(normalized_url)
                queue.append(normalized_url)

    write_report(output_dir, scraped_pages, asset_cache, failed_pages)
    print(f"Scraped {len(scraped_pages)} pages into {output_dir}")
    print(f"Downloaded {len(asset_cache)} assets")
    if failed_pages:
        print(f"Failed pages: {len(failed_pages)}. See {output_dir / 'scrape-report.md'}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Labs website content into Markdown files.")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help=f"Output folder to delete and recreate. Defaults to {OUTPUT_DIR}",
    )
    return parser.parse_args()


def recreate_output_dir(output_dir: Path) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def load_sitemap_urls(client: RateLimitedClient, sitemap_url: str) -> set[str]:
    seen_sitemaps: set[str] = set()
    page_urls: set[str] = set()

    def load(url: str) -> None:
        if url in seen_sitemaps:
            return
        seen_sitemaps.add(url)

        response = client.get(url)
        root = ET.fromstring(response.content)
        root_name = local_name(root.tag)

        if root_name == "sitemapindex":
            for loc in xml_loc_values(root):
                load(loc)
            return

        if root_name == "urlset":
            page_urls.update(xml_loc_values(root))

    load(sitemap_url)
    return page_urls


def scrape_page(
    client: RateLimitedClient,
    page_url: str,
    output_dir: Path,
    asset_cache: dict[str, Path],
) -> set[str]:
    print(f"Scraping {page_url}")
    response = client.get(page_url)
    soup = BeautifulSoup(response.text, "html.parser")
    content = soup.select_one(SELECTORS["content"])
    if content is None:
        raise ValueError(f"Missing content selector: {SELECTORS['content']}")

    normalize_video_embeds(content)
    normalize_image_markup(content)
    cleanup_markup_for_markdown(content)
    discovered_pages = discover_page_links(content, page_url)
    rewrite_content_links(content, page_url, output_dir, asset_cache, client)

    page_path = page_output_path(output_dir, page_url)
    page_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = html_to_markdown(
        str(content),
        heading_style="ATX",
        bullets="-",
        strip=["script", "style", "noscript"],
    ).strip()
    page_path.write_text(markdown + "\n", encoding="utf-8")

    return discovered_pages


def discover_page_links(content: Tag, page_url: str) -> set[str]:
    discovered: set[str] = set()
    for tag in content.select(SELECTORS["links"]):
        href = tag.get("href")
        if not href:
            continue
        absolute_url = normalize_page_url(urljoin(page_url, href))
        if is_target_page(absolute_url):
            discovered.add(absolute_url)
    return discovered


def normalize_image_markup(content: Tag) -> None:
    """Prefer original img[data-src] URLs and simplify pictures for Markdown output."""
    for img in content.select("img"):
        promote_lazy_image_source(img)

    for picture in content.select("picture"):
        img = picture.find("img")
        if not isinstance(img, Tag):
            continue

        for source in picture.find_all("source"):
            source.decompose()

        picture.replace_with(img)


def promote_lazy_image_source(img: Tag) -> None:
    data_src = img.get("data-src")
    if isinstance(data_src, str) and data_src.strip():
        img["src"] = data_src.strip()

    for attribute in ("srcset", "data-src", "data-srcset", "data-sizes", "sizes"):
        if attribute in img.attrs:
            del img.attrs[attribute]


def cleanup_markup_for_markdown(content: Tag) -> None:
    for svg in content.select("svg"):
        svg.decompose()

    for instruction in content.find_all(string=lambda text: isinstance(text, ProcessingInstruction)):
        instruction.extract()


def normalize_video_embeds(content: Tag) -> None:
    for player in content.select(".youtube-player"):
        if not isinstance(player, Tag):
            continue

        video_id = youtube_video_id(player)
        if not video_id:
            continue

        youtube_url = f"https://www.youtube.com/watch?v={video_id}"
        poster = player.select_one(".youtube-player__poster img") or player.select_one("img")
        soup = BeautifulSoup("", "html.parser")

        if isinstance(poster, Tag):
            link = soup.new_tag("a", href=youtube_url)
            link.append(cast(Tag, poster.extract()))
            player.replace_with(link)
            continue

        paragraph = soup.new_tag("p")
        link = soup.new_tag("a", href=youtube_url)
        link.string = "YouTube video"
        paragraph.append(link)
        player.replace_with(paragraph)


def youtube_video_id(player: Tag) -> str | None:
    for selector in ('[data-id]', 'meta[itemprop="embedURL"]'):
        element = player.select_one(selector)
        if not isinstance(element, Tag):
            continue

        raw_value = element.get("data-id") or element.get("content")
        if isinstance(raw_value, str) and raw_value.strip():
            return extract_youtube_video_id(raw_value.strip())

    return None


def extract_youtube_video_id(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.netloc:
        path_parts = [part for part in parsed.path.split("/") if part]
        if parsed.netloc.endswith("youtu.be") and path_parts:
            return path_parts[0]
        if "youtube" in parsed.netloc and path_parts:
            if path_parts[0] in {"embed", "v", "shorts"} and len(path_parts) > 1:
                return path_parts[1]
            query_id = parse_qs(parsed.query).get("v", [""])[0]
            if query_id:
                return query_id

    if re.fullmatch(r"[A-Za-z0-9_-]{6,}", value):
        return value

    return None


def rewrite_content_links(
    content: Tag,
    page_url: str,
    output_dir: Path,
    asset_cache: dict[str, Path],
    client: RateLimitedClient,
) -> None:
    current_page_path = page_output_path(output_dir, page_url)

    for tag in content.select(",".join(SELECTORS["assets"])):
        if not isinstance(tag, Tag):
            continue

        rewrite_attribute_url(tag, "src", page_url, current_page_path, output_dir, asset_cache, client)
        rewrite_attribute_url(tag, "href", page_url, current_page_path, output_dir, asset_cache, client)
        rewrite_attribute_url(tag, "data", page_url, current_page_path, output_dir, asset_cache, client)
        rewrite_srcset(tag, page_url, current_page_path, output_dir, asset_cache, client)


def rewrite_attribute_url(
    tag: Tag,
    attribute: str,
    page_url: str,
    current_page_path: Path,
    output_dir: Path,
    asset_cache: dict[str, Path],
    client: RateLimitedClient,
) -> None:
    value = tag.get(attribute)
    if not value or not isinstance(value, str):
        return

    rewritten = rewrite_url(value, page_url, current_page_path, output_dir, asset_cache, client)
    if rewritten is not None:
        tag[attribute] = rewritten
    elif attribute == "href":
        absolute_url = absolute_url_for_unhandled_relative_link(value, page_url)
        if absolute_url is not None:
            tag[attribute] = absolute_url


def rewrite_srcset(
    tag: Tag,
    page_url: str,
    current_page_path: Path,
    output_dir: Path,
    asset_cache: dict[str, Path],
    client: RateLimitedClient,
) -> None:
    value = tag.get("srcset")
    if not value or not isinstance(value, str):
        return

    rewritten_candidates = []
    changed = False
    for candidate in split_srcset(value):
        if not candidate:
            continue
        url, descriptor = split_srcset_candidate(candidate)
        rewritten_url = rewrite_url(url, page_url, current_page_path, output_dir, asset_cache, client)
        if rewritten_url is not None:
            changed = True
            url = rewritten_url
        rewritten_candidates.append(f"{url} {descriptor}".strip())

    if changed:
        tag["srcset"] = ", ".join(rewritten_candidates)


def rewrite_url(
    value: str,
    page_url: str,
    current_page_path: Path,
    output_dir: Path,
    asset_cache: dict[str, Path],
    client: RateLimitedClient,
) -> str | None:
    if is_ignored_url(value):
        return None

    absolute_url = urljoin(page_url, value)
    normalized_page = normalize_page_url(absolute_url)

    if should_download_asset(absolute_url):
        asset_path = download_asset(client, absolute_url, output_dir, asset_cache)
        return relative_path(current_page_path, asset_path)

    if is_target_page(normalized_page):
        target_page_path = page_output_path(output_dir, normalized_page)
        return relative_path(current_page_path, target_page_path)

    return None


def download_asset(
    client: RateLimitedClient,
    asset_url: str,
    output_dir: Path,
    asset_cache: dict[str, Path],
) -> Path:
    cache_key = normalize_asset_url(asset_url)
    if cache_key in asset_cache:
        return asset_cache[cache_key]

    response = client.get(cache_key)
    asset_path = asset_output_path(
        output_dir,
        cache_key,
        response.headers.get("content-type", ""),
        asset_cache,
    )
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_bytes(response.content)
    asset_cache[cache_key] = asset_path
    return asset_path


def should_download_asset(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if origin(url) not in ALLOWED_ASSET_ORIGINS:
        return False

    extension = Path(parsed.path).suffix.lower()
    return extension in ASSET_EXTENSIONS


def is_target_page(url: str) -> bool:
    normalized = normalize_page_url(url)
    return any(normalized.startswith(prefix) for prefix in TARGET_PREFIXES)


def is_ignored_url(url: str) -> bool:
    lowered = url.strip().lower()
    return (
        not lowered
        or lowered.startswith("#")
        or lowered.startswith("mailto:")
        or lowered.startswith("tel:")
        or lowered.startswith("javascript:")
        or lowered.startswith("data:")
    )


def absolute_url_for_unhandled_relative_link(url: str, page_url: str) -> str | None:
    if is_ignored_url(url):
        return None

    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc:
        return None

    return urljoin(page_url, url)


def normalize_page_url(url: str) -> str:
    url_without_fragment = urldefrag(url)[0]
    parsed = urlparse(url_without_fragment)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def normalize_asset_url(url: str) -> str:
    return urldefrag(url)[0]


def page_output_path(output_dir: Path, page_url: str) -> Path:
    parsed = urlparse(page_url)
    parts = safe_path_parts(parsed.path)

    if not parts:
        return output_dir / "index.md"

    last_part = parts[-1]
    suffix = Path(last_part).suffix
    if suffix:
        parts[-1] = Path(last_part).with_suffix(".md").name
        return output_dir.joinpath(*parts)

    return output_dir.joinpath(*parts, "index.md")


def asset_output_path(
    output_dir: Path,
    asset_url: str,
    content_type: str,
    asset_cache: dict[str, Path] | None = None,
) -> Path:
    parsed = urlparse(asset_url)
    parts = safe_path_parts(parsed.path)
    filename = parts[-1] if parts else "asset"
    suffix = Path(filename).suffix
    if not suffix:
        guessed_extension = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed_extension:
            filename = f"{filename}{guessed_extension}"

    if parsed.query:
        query_hash = hashlib.sha256(parsed.query.encode("utf-8")).hexdigest()[:10]
        path_filename = Path(filename)
        filename = f"{path_filename.stem}-{query_hash}{path_filename.suffix}"

    asset_path = output_dir / MEDIA_DIR_NAME / filename
    if asset_cache and asset_path in asset_cache.values():
        path_filename = Path(filename)
        asset_hash = hashlib.sha256(asset_url.encode("utf-8")).hexdigest()[:10]
        asset_path = output_dir / MEDIA_DIR_NAME / f"{path_filename.stem}-{asset_hash}{path_filename.suffix}"

    return asset_path


def safe_path_parts(path: str) -> list[str]:
    parts = []
    for part in unquote(path).split("/"):
        if not part or part in {".", ".."}:
            continue
        cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", part)
        parts.append(cleaned)
    return parts


def relative_path(from_file: Path, to_file: Path) -> str:
    return Path(os.path.relpath(to_file.resolve(), from_file.resolve().parent)).as_posix()


def origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_loc_values(root: ET.Element) -> Iterable[str]:
    for element in root.iter():
        if local_name(element.tag) == "loc" and element.text:
            yield element.text.strip()


def split_srcset(srcset: str) -> list[str]:
    return [candidate.strip() for candidate in srcset.split(",")]


def split_srcset_candidate(candidate: str) -> tuple[str, str]:
    parts = candidate.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def write_report(
    output_dir: Path,
    scraped_pages: set[str],
    asset_cache: dict[str, Path],
    failed_pages: list[tuple[str, str]],
) -> None:
    lines = [
        "# Scrape Report",
        "",
        f"- Pages scraped: {len(scraped_pages)}",
        f"- Assets downloaded: {len(asset_cache)}",
        f"- Failed pages: {len(failed_pages)}",
        "",
        "## Pages",
        "",
    ]
    lines.extend(f"- {url}" for url in sorted(scraped_pages))

    if failed_pages:
        lines.extend(["", "## Failed Pages", ""])
        lines.extend(f"- {url}: {error}" for url, error in failed_pages)

    lines.extend(["", "## Assets", ""])
    lines.extend(f"- {url} -> {path.relative_to(output_dir)}" for url, path in sorted(asset_cache.items()))

    (output_dir / "scrape-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
