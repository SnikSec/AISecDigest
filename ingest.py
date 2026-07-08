#!/usr/bin/env python3
"""Fetch all sources.yaml feeds + GitHub Security Advisories, normalize to one schema.

Two source types are supported per sources.yaml entry (`type:`, default "rss"):
  - rss: standard RSS/Atom, fetched with feedparser.
  - wp_rest_api: WordPress sites with RSS disabled (e.g. Semperis, whose
    /feed/ and /blog/feed/ both redirect to HTML). Fetched via
    wp-json/wp/v2/posts; author name comes from each post's
    yoast_head_json.author since the wp-json .../users endpoint is blocked.

Output record schema (dict):
    title, url, source, published_at (ISO8601 UTC str or None), summary,
    bucket, tier, is_named_author, named_author
"""
import argparse
import html
import json
import re
import sys
from calendar import timegm
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import yaml

USER_AGENT = "AISecDigest/1.0 (+https://github.com/; daily AI-security news bot)"
GITHUB_API = "https://api.github.com/advisories"


def load_sources(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    # GitHub Advisory descriptions are raw Markdown (not HTML), and their
    # ATX headers ("### Summary") read as nested headers once embedded
    # under render.py's own "### [title]" heading - strip the leading hashes.
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def struct_time_to_iso(st) -> str | None:
    if not st:
        return None
    return datetime.fromtimestamp(timegm(st), tz=timezone.utc).isoformat()


def source_key(cfg: dict) -> str:
    """Unique key for a feed/api entry, independent of source type."""
    return cfg.get("feed_url") or cfg.get("api_url")


def build_feed_registry(config: dict) -> tuple[dict, dict]:
    """Returns (bucket_sources, author_sources) keyed by source_key(cfg)."""
    bucket_sources = {}
    for bucket_name, feeds in config["buckets"].items():
        for feed in feeds:
            bucket_sources[source_key(feed)] = {
                "bucket": bucket_name,
                "name": feed["name"],
                "tier": feed.get("tier", 2),
                "type": feed.get("type", "rss"),
                "url": source_key(feed),
                "require_both_axes": feed.get("require_both_axes", False),
            }

    author_sources = {}
    for author in config["named_authors"]:
        author_sources.setdefault(source_key(author), []).append(
            {**author, "type": author.get("type", "rss"), "url": source_key(author)}
        )

    return bucket_sources, author_sources


def entry_matches_author(entry: dict, match: str | None) -> bool:
    if match is None:
        return True
    haystack = " ".join(
        [entry.get("author", ""), entry.get("title", ""), entry.get("summary", "")]
    ).lower()
    return match.lower() in haystack


def fetch_rss_entries(feed_url: str) -> list[dict]:
    parsed = feedparser.parse(feed_url, agent=USER_AGENT)
    if parsed.get("bozo") and not parsed.entries:
        print(f"  WARN: failed to parse {feed_url}: {parsed.get('bozo_exception')}", file=sys.stderr)
        return []
    entries = []
    for e in parsed.entries:
        entries.append(
            {
                "title": strip_html(e.get("title", "")),
                "url": e.get("link", ""),
                "author": e.get("author", ""),
                "published_at": struct_time_to_iso(
                    e.get("published_parsed") or e.get("updated_parsed")
                ),
                "summary": strip_html(e.get("summary", "") or e.get("description", "")),
            }
        )
    return entries


def fetch_wp_rest_api_entries(api_url: str, per_page: int = 20) -> list[dict]:
    try:
        resp = requests.get(
            api_url,
            params={"per_page": per_page, "orderby": "date", "order": "desc"},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20,
        )
        resp.raise_for_status()
        posts = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  WARN: failed to fetch wp-json {api_url}: {exc}", file=sys.stderr)
        return []

    entries = []
    for post in posts:
        date_gmt = post.get("date_gmt")
        published_at = None
        if date_gmt:
            published_at = (
                datetime.fromisoformat(date_gmt).replace(tzinfo=timezone.utc).isoformat()
            )
        yoast = post.get("yoast_head_json", {}) or {}
        entries.append(
            {
                "title": strip_html(post.get("title", {}).get("rendered", "")),
                "url": post.get("link", ""),
                "author": yoast.get("author", ""),
                "published_at": published_at,
                "summary": strip_html(post.get("excerpt", {}).get("rendered", "")),
            }
        )
    return entries


def fetch_entries(source_type: str, url: str) -> list[dict]:
    if source_type == "wp_rest_api":
        return fetch_wp_rest_api_entries(url)
    return fetch_rss_entries(url)


def ingest_feeds(config: dict) -> list[dict]:
    bucket_sources, author_sources = build_feed_registry(config)
    all_keys = set(bucket_sources) | set(author_sources)

    records = []
    for key in sorted(all_keys):
        bucket_cfg = bucket_sources.get(key)
        authors_cfg = author_sources.get(key, [])
        source_type = bucket_cfg["type"] if bucket_cfg else authors_cfg[0]["type"]
        label = bucket_cfg["name"] if bucket_cfg else authors_cfg[0]["name"]
        print(f"Fetching: {label} ({key}) [{source_type}]", file=sys.stderr)

        entries = fetch_entries(source_type, key)
        print(f"  -> {len(entries)} entries", file=sys.stderr)

        for e in entries:
            is_named_author = False
            named_author = None
            bucket = bucket_cfg["bucket"] if bucket_cfg else None

            for author_cfg in authors_cfg:
                if entry_matches_author(e, author_cfg.get("match")):
                    is_named_author = True
                    named_author = author_cfg["name"]
                    if bucket is None:
                        bucket = author_cfg["default_bucket"]
                    break

            if bucket is None:
                continue  # standalone author feed with no match and no bucket fallback

            source_name = bucket_cfg["name"] if bucket_cfg else f"{named_author} ({key})"
            tier = bucket_cfg["tier"] if bucket_cfg else 1

            records.append(
                {
                    "title": e["title"],
                    "url": e["url"],
                    "source": source_name,
                    "published_at": e["published_at"],
                    "summary": e["summary"],
                    "bucket": bucket,
                    "tier": tier,
                    "is_named_author": is_named_author,
                    "named_author": named_author,
                    "require_both_axes": bucket_cfg.get("require_both_axes", False) if bucket_cfg else False,
                }
            )

    return records


def ingest_github_advisories(config: dict) -> list[dict]:
    adv_cfg = config.get("github_advisories", {})
    ecosystems = adv_cfg.get("ecosystems", [])
    keywords = [k.lower() for k in adv_cfg.get("ml_package_keywords", [])]

    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    records = []
    for ecosystem in ecosystems:
        print(f"Fetching GitHub advisories: ecosystem={ecosystem}", file=sys.stderr)
        try:
            resp = requests.get(
                GITHUB_API,
                params={"ecosystem": ecosystem, "per_page": 100, "sort": "published", "direction": "desc"},
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"  WARN: GitHub advisories fetch failed for {ecosystem}: {exc}", file=sys.stderr)
            continue

        advisories = resp.json()
        kept = 0
        for adv in advisories:
            haystack = " ".join(
                [adv.get("summary", "") or ""]
                + [v.get("package", {}).get("name", "") or "" for v in adv.get("vulnerabilities", []) or []]
            ).lower()
            if not any(kw in haystack for kw in keywords):
                continue
            kept += 1
            records.append(
                {
                    "title": adv.get("summary", "").strip(),
                    "url": adv.get("html_url", ""),
                    "source": "GitHub Security Advisories",
                    "published_at": adv.get("published_at"),
                    "summary": strip_html(adv.get("description", "") or "")[:1000],
                    "bucket": "ai_security",
                    "tier": 1,
                    "is_named_author": False,
                    "named_author": None,
                    "require_both_axes": False,
                }
            )
        print(f"  -> {kept}/{len(advisories)} matched ML/LLM package keywords", file=sys.stderr)

    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default="sources.yaml", type=Path)
    parser.add_argument("--out", default="data/raw_items.json", type=Path)
    args = parser.parse_args()

    config = load_sources(args.sources)

    records = ingest_feeds(config)
    records += ingest_github_advisories(config)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    print(f"\nWrote {len(records)} raw items to {args.out}", file=sys.stderr)
    by_bucket = {}
    for r in records:
        by_bucket[r["bucket"]] = by_bucket.get(r["bucket"], 0) + 1
    for bucket, count in sorted(by_bucket.items()):
        print(f"  {bucket}: {count}", file=sys.stderr)
    named = sum(1 for r in records if r["is_named_author"])
    print(f"  named_authors matched: {named}", file=sys.stderr)


if __name__ == "__main__":
    main()
