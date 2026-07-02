#!/usr/bin/env python3
"""Build an RSS 2.0 + Atom feed for the Anthropic news page.

Data source is Anthropic's sitemap (stable, machine-readable) rather than the
Next.js news page (hashed CSS class names, brittle). Each news article page is
fetched once to pull its Open Graph metadata (title/description/image).

State lives in feed_state.json so publish dates are stable ("first-seen"):
- On the very first run (empty state) each item's published date is seeded from
  the sitemap <lastmod>.
- On later runs, a newly-appeared article is stamped with the current time.
Existing items keep whatever date they were first given, so the feed never
reorders itself just because Anthropic edited an old page.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

from feedgen.feed import FeedGenerator

# --- Configuration ----------------------------------------------------------

SITEMAP_URL = "https://www.anthropic.com/sitemap.xml"
SITE_BASE = "https://www.anthropic.com"
# Public GitHub Pages URL for this feed. Fill in after the repo is created, e.g.
# "https://elmer.github.io/claude-watcher/". Trailing slash required.
FEED_PAGE_URL = os.environ.get("FEED_PAGE_URL", "https://example.github.io/claude-watcher/")

MAX_ITEMS = 50  # newest N items included in the generated feeds
REQUEST_TIMEOUT = 20  # seconds per HTTP request
FETCH_DELAY = 0.5  # polite pause between article fetches (seconds)
USER_AGENT = (
    "claude-watcher/1.0 (+https://github.com/; Anthropic news feed builder)"
)

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "feed_state.json")
PUBLIC_DIR = os.path.join(HERE, "public")

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


# --- HTTP -------------------------------------------------------------------

def fetch(url: str, retries: int = 3) -> bytes:
    """Fetch a URL with a real User-Agent and simple retry/backoff."""
    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as err:  # pragma: no cover
            last_err = err
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last_err}")


# --- Sitemap parsing --------------------------------------------------------

def slug_from_url(url: str) -> str | None:
    """Return the news slug for a /news/<slug> URL, else None."""
    prefix = f"{SITE_BASE}/news/"
    if not url.startswith(prefix):
        return None
    slug = url[len(prefix):].strip("/")
    # Exclude nested paths and the bare index; we want single-segment slugs.
    if not slug or "/" in slug:
        return None
    return slug


def parse_sitemap(data: bytes) -> dict[str, dict]:
    """Return {slug: {"url", "lastmod"}} for every news article in the sitemap."""
    root = ET.fromstring(data)
    items: dict[str, dict] = {}
    for url_el in root.findall("sm:url", SITEMAP_NS):
        loc_el = url_el.find("sm:loc", SITEMAP_NS)
        if loc_el is None or not loc_el.text:
            continue
        loc = loc_el.text.strip()
        slug = slug_from_url(loc)
        if slug is None:
            continue
        lastmod_el = url_el.find("sm:lastmod", SITEMAP_NS)
        lastmod = lastmod_el.text.strip() if lastmod_el is not None and lastmod_el.text else None
        items[slug] = {"url": loc, "lastmod": lastmod}
    return items


# --- Article metadata (Open Graph) ------------------------------------------

class MetaParser(HTMLParser):
    """Collect og:* / meta description / <title> from an article page."""

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self._in_title = False
        self.title_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag != "meta":
            return
        a = {k: (v or "") for k, v in attrs}
        key = a.get("property") or a.get("name")
        content = a.get("content")
        if key and content and key not in self.meta:
            self.meta[key] = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_text += data


def enrich(url: str, slug: str) -> dict:
    """Fetch an article page and extract title/summary/image. Degrades gracefully."""
    fallback_title = slug.replace("-", " ").title()
    try:
        parser = MetaParser()
        parser.feed(fetch(url).decode("utf-8", "replace"))
        m = parser.meta
        title = m.get("og:title") or parser.title_text.strip() or fallback_title
        summary = m.get("og:description") or m.get("description") or ""
        image = m.get("og:image") or ""
    except Exception as err:  # noqa: BLE001 - one bad page must not fail the run
        print(f"  ! enrich failed for {slug}: {err}", file=sys.stderr)
        title, summary, image = fallback_title, "", ""
    return {
        "title": html.unescape(title).strip(),
        "summary": html.unescape(summary).strip(),
        "image": image.strip(),
    }


# --- Dates ------------------------------------------------------------------

def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def ensure_aware(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        return now_utc()
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


# --- State ------------------------------------------------------------------

def load_state() -> dict[str, dict]:
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state: dict[str, dict]) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


# --- Feed generation --------------------------------------------------------

def build_feeds(state: dict[str, dict]) -> None:
    os.makedirs(PUBLIC_DIR, exist_ok=True)

    items = sorted(
        state.values(),
        key=lambda it: ensure_aware(parse_iso(it.get("published"))),
        reverse=True,
    )[:MAX_ITEMS]

    fg = FeedGenerator()
    fg.id(FEED_PAGE_URL)
    fg.title("Anthropic News (unofficial)")
    fg.subtitle("Announcements from anthropic.com/news, built hourly from the sitemap.")
    fg.author({"name": "Anthropic"})
    fg.link(href=f"{SITE_BASE}/news", rel="alternate")
    fg.link(href=f"{FEED_PAGE_URL}feed.xml", rel="self")
    fg.language("en")
    fg.logo(f"{SITE_BASE}/favicon.ico")
    if items:
        fg.updated(ensure_aware(parse_iso(items[0].get("published"))))

    for it in items:
        published = ensure_aware(parse_iso(it.get("published")))
        updated = ensure_aware(parse_iso(it.get("updated")) or published)
        fe = fg.add_entry(order="append")  # preserve our newest-first ordering
        fe.id(it["url"])
        fe.guid(it["url"], permalink=True)
        fe.title(it.get("title") or it["url"])
        fe.link(href=it["url"])
        if it.get("summary"):
            fe.description(it["summary"])
            fe.summary(it["summary"])
        fe.published(published)
        fe.updated(updated)
        if it.get("image"):
            # RSS enclosure + Atom link; type is a reasonable default for OG images.
            fe.enclosure(it["image"], 0, "image/jpeg")

    fg.rss_file(os.path.join(PUBLIC_DIR, "feed.xml"), pretty=True)
    fg.atom_file(os.path.join(PUBLIC_DIR, "atom.xml"), pretty=True)
    write_index(items)


def write_index(items: list[dict]) -> None:
    rows = "\n".join(
        f'      <li><a href="{html.escape(it["url"])}">{html.escape(it.get("title") or it["url"])}</a></li>'
        for it in items[:20]
    )
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Anthropic News (unofficial feed)</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 42rem; margin: 3rem auto; padding: 0 1rem; line-height: 1.5; }}
    code {{ background: #f2f2f2; padding: 0.1rem 0.3rem; border-radius: 4px; }}
    a {{ color: #cc785c; }}
  </style>
</head>
<body>
  <h1>Anthropic News — unofficial feed</h1>
  <p>An automatically generated feed of <a href="{SITE_BASE}/news">anthropic.com/news</a>, rebuilt hourly.</p>
  <p>Subscribe:
    <a href="feed.xml">RSS 2.0</a> &middot;
    <a href="atom.xml">Atom</a>
  </p>
  <h2>Latest</h2>
  <ul>
{rows}
  </ul>
  <p><small>Not affiliated with Anthropic. Data from their public sitemap.</small></p>
</body>
</html>
"""
    with open(os.path.join(PUBLIC_DIR, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)


# --- Main -------------------------------------------------------------------

def main() -> int:
    print(f"Fetching sitemap: {SITEMAP_URL}")
    sitemap = parse_sitemap(fetch(SITEMAP_URL))
    print(f"  found {len(sitemap)} news articles")

    state = load_state()
    first_run = len(state) == 0
    new_slugs = [s for s in sitemap if s not in state]
    print(f"  {len(new_slugs)} new article(s){' (first run — seeding from lastmod)' if first_run else ''}")

    for i, slug in enumerate(new_slugs, 1):
        info = sitemap[slug]
        print(f"  [{i}/{len(new_slugs)}] enrich {slug}")
        meta = enrich(info["url"], slug)
        if first_run:
            published = info["lastmod"] or now_utc().isoformat()
        else:
            published = now_utc().isoformat()
        state[slug] = {
            "url": info["url"],
            "published": published,
            "updated": info["lastmod"] or published,
            **meta,
        }
        time.sleep(FETCH_DELAY)

    # Refresh the "updated" timestamp for existing items from the sitemap.
    for slug, info in sitemap.items():
        if slug in state and info.get("lastmod"):
            state[slug]["updated"] = info["lastmod"]

    save_state(state)
    build_feeds(state)
    print(f"Wrote feeds to {PUBLIC_DIR} ({min(len(state), MAX_ITEMS)} items)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
