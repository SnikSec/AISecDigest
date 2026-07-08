#!/usr/bin/env python3
"""Dedupe, apply a recency window + per-bucket keyword gate, score, and cap.

Recency window: tries a 24h cutoff first (matching the daily cron cadence);
if that leaves fewer than --min-items eligible candidates, widens to 48h to
backfill. This is deterministic given fixed inputs (same items in -> same
window choice out), it just means a slow-news run may pull in slightly
older items rather than shipping a half-empty digest.

Dedup happens in two passes: (1) within this run, by normalized URL then by
normalized title, preferring the higher-tier (lower tier number) source on
collision; (2) against data/seen.json, a small persisted store of
previously-dispatched item URLs, so a story that's still inside the lookback
window on day 2 doesn't get re-sent. This run's selected items are appended
to that store (pruned to a 7-day trailing window to bound file growth).

named_authors items (is_named_author=True) bypass the keyword gate and are
guaranteed a slot in the output (subject only to recency/dedupe/seen), since
the whole point of that list is "always include, regardless of topic match."

Score = tier weight + keyword-density bonus + score_bonus_terms bonus
      (+ a flat named-author bonus, so curated authors sort near the top
        without needing to out-keyword the gate they're exempt from).
This is a pure function of the input data - no LLM calls here. summarize.py
does the LLM-based re-score later, on top of this.
"""
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

PRIMARY_WINDOW_HOURS = 24
FALLBACK_WINDOW_HOURS = 48
MIN_ITEMS = 15
MAX_ITEMS = 25
MAX_PER_SOURCE = 6
SEEN_STORE_DAYS = 7

TIER_WEIGHTS = {1: 30, 2: 20, 3: 10}
KEYWORD_HIT_POINTS = 5
KEYWORD_HIT_CAP = 25
BONUS_HIT_POINTS = 15
BONUS_HIT_CAP = 45
NAMED_AUTHOR_BONUS = 20


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_yaml(path: Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip().lower())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


_TERM_PATTERN_CACHE: dict[str, re.Pattern] = {}


def count_term_hits(haystack: str, terms: list[str]) -> int:
    """Word-boundary, case-insensitive term matching.

    Plain substring checks false-positive badly on short terms like "AI" -
    it matches inside "email", "container", "maintain", "domain", etc.
    \\b...\\b avoids that since hyphens/spaces/punctuation aren't word chars.
    """
    hits = 0
    for term in terms:
        pattern = _TERM_PATTERN_CACHE.get(term)
        if pattern is None:
            pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
            _TERM_PATTERN_CACHE[term] = pattern
        if pattern.search(haystack):
            hits += 1
    return hits


def normalize_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[^a-z0-9\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def dedupe(records: list[dict]) -> list[dict]:
    by_url: dict[str, dict] = {}
    for r in records:
        key = normalize_url(r["url"])
        if not key:
            continue
        existing = by_url.get(key)
        if existing is None or r["tier"] < existing["tier"]:
            by_url[key] = r

    by_title: dict[str, dict] = {}
    for r in by_url.values():
        key = normalize_title(r["title"])
        if not key:
            by_title[id(r)] = r
            continue
        existing = by_title.get(key)
        if existing is None or r["tier"] < existing["tier"]:
            by_title[key] = r

    return list(by_title.values())


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = load_json(path)
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_STORE_DAYS)
    live = set()
    for entry in data:
        try:
            seen_at = datetime.fromisoformat(entry["seen_at"])
        except (KeyError, ValueError):
            continue
        if seen_at >= cutoff:
            live.add(entry["url"])
    return live


def save_seen(path: Path, previously_seen: set[str], new_urls: list[str]):
    now = datetime.now(timezone.utc).isoformat()
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_STORE_DAYS)

    existing = []
    if path.exists():
        for entry in load_json(path):
            try:
                seen_at = datetime.fromisoformat(entry["seen_at"])
            except (KeyError, ValueError):
                continue
            if seen_at >= cutoff:
                existing.append(entry)

    existing_urls = {e["url"] for e in existing}
    for url in new_urls:
        if url not in existing_urls:
            existing.append({"url": url, "seen_at": now})
            existing_urls.add(url)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)


def within_window(record: dict, window_hours: int, now: datetime) -> bool:
    if not record.get("published_at"):
        return False
    try:
        published = datetime.fromisoformat(record["published_at"])
    except ValueError:
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return (now - published) <= timedelta(hours=window_hours) and published <= now + timedelta(minutes=5)


def keyword_gate_pass(record: dict, keyword_gate: dict) -> tuple[bool, int]:
    gate_cfg = keyword_gate.get(record["bucket"], [])
    haystack = f"{record['title']} {record['summary']}"

    if isinstance(gate_cfg, dict):
        # Split ai_terms/security_terms gate (currently only ai_security).
        # Every ai_security source is already a security source by
        # construction (vendor blog, security-tagged HN query, GH
        # advisories) *except* the arXiv feeds, which aren't pre-filtered
        # for either axis. So the invariant is always "must match an
        # ai_term" (that's what makes it belong in an *AI*-security
        # digest), plus "must also match a security_term" only for sources
        # that aren't already guaranteed to be security content.
        ai_hits = count_term_hits(haystack, gate_cfg.get("ai_terms", []))
        sec_hits = count_term_hits(haystack, gate_cfg.get("security_terms", []))
        hits = ai_hits + sec_hits
        if record.get("require_both_axes"):
            return (ai_hits > 0 and sec_hits > 0), hits
        return ai_hits > 0, hits

    hits = count_term_hits(haystack, gate_cfg)
    return hits > 0, hits


def score_record(record: dict, keyword_hits: int, score_bonus_terms: list[str]) -> int:
    tier_score = TIER_WEIGHTS.get(record["tier"], 10)
    keyword_score = min(keyword_hits * KEYWORD_HIT_POINTS, KEYWORD_HIT_CAP)

    haystack = f"{record['title']} {record['summary']}"
    bonus_hits = count_term_hits(haystack, score_bonus_terms)
    bonus_score = min(bonus_hits * BONUS_HIT_POINTS, BONUS_HIT_CAP)

    author_score = NAMED_AUTHOR_BONUS if record["is_named_author"] else 0

    return tier_score + keyword_score + bonus_score + author_score


def select_for_window(
    records: list[dict],
    window_hours: int,
    now: datetime,
    keyword_gate: dict,
    score_bonus_terms: list[str],
    max_items: int,
    max_per_source: int,
) -> list[dict]:
    windowed = [r for r in records if within_window(r, window_hours, now)]

    named_pool = []
    regular_pool = []
    for r in windowed:
        if r["is_named_author"]:
            _, hits = keyword_gate_pass(r, keyword_gate)
            r = {**r, "score": score_record(r, hits, score_bonus_terms)}
            named_pool.append(r)
        else:
            passed, hits = keyword_gate_pass(r, keyword_gate)
            if not passed:
                continue
            r = {**r, "score": score_record(r, hits, score_bonus_terms)}
            regular_pool.append(r)

    named_pool.sort(key=lambda r: r["score"], reverse=True)
    regular_pool.sort(key=lambda r: r["score"], reverse=True)

    # A high-volume bucket (arXiv floods ai_security) can otherwise fill the
    # entire cap on score alone, shutting out buckets that have real but
    # sparser content. Reserve one slot for the top item of any bucket not
    # already represented in named_pool, before filling the rest by score.
    represented_buckets = {r["bucket"] for r in named_pool}
    regular_by_bucket: dict[str, list[dict]] = {}
    for r in regular_pool:
        regular_by_bucket.setdefault(r["bucket"], []).append(r)

    reserved = []
    for bucket, items in regular_by_bucket.items():
        if bucket not in represented_buckets and items:
            reserved.append(items[0])
            represented_buckets.add(bucket)

    reserved_urls = {r["url"] for r in reserved}
    leftover = [r for r in regular_pool if r["url"] not in reserved_urls]

    # A single high-volume source (arXiv cs.AI) can still fill every
    # remaining slot on score alone even with bucket reservation, since its
    # bonus-term-dense abstracts systematically outscore shorter vendor-blog
    # posts. Cap contributions per source while filling the rest by score,
    # so e.g. The Hacker News items that passed the gate get a real shot
    # instead of being permanently outbid by arXiv.
    per_source_count = Counter(r["source"] for r in named_pool + reserved)
    remaining_slots = max(max_items - len(named_pool) - len(reserved), 0)
    fill = []
    for r in leftover:  # already sorted desc by score
        if len(fill) >= remaining_slots:
            break
        if per_source_count[r["source"]] >= max_per_source:
            continue
        fill.append(r)
        per_source_count[r["source"]] += 1

    selected = named_pool[:max_items] + reserved + fill
    selected.sort(key=lambda r: r["score"], reverse=True)
    return selected[:max_items]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_path", default="data/raw_items.json", type=Path)
    parser.add_argument("--sources", default="sources.yaml", type=Path)
    parser.add_argument("--out", default="data/filtered_items.json", type=Path)
    parser.add_argument("--seen-store", default="data/seen.json", type=Path)
    parser.add_argument("--min-items", type=int, default=MIN_ITEMS)
    parser.add_argument("--max-items", type=int, default=MAX_ITEMS)
    parser.add_argument("--max-per-source", type=int, default=MAX_PER_SOURCE)
    parser.add_argument("--no-seen-filter", action="store_true", help="Ignore seen.json (useful for testing)")
    parser.add_argument("--no-update-seen", action="store_true", help="Don't write seen.json (useful for testing)")
    args = parser.parse_args()

    min_items, max_items, max_per_source = args.min_items, args.max_items, args.max_per_source

    config = load_yaml(args.sources)
    keyword_gate = config.get("keyword_gate", {})
    score_bonus_terms = config.get("score_bonus_terms", [])

    raw = load_json(args.in_path)
    print(f"Loaded {len(raw)} raw items", file=sys.stderr)

    deduped = dedupe(raw)
    print(f"After dedupe: {len(deduped)} items", file=sys.stderr)

    seen = set() if args.no_seen_filter else load_seen(args.seen_store)
    if seen:
        before = len(deduped)
        deduped = [r for r in deduped if normalize_url(r["url"]) not in seen]
        print(f"After seen-store filter: {len(deduped)} items (dropped {before - len(deduped)})", file=sys.stderr)

    now = datetime.now(timezone.utc)

    selected = select_for_window(deduped, PRIMARY_WINDOW_HOURS, now, keyword_gate, score_bonus_terms, max_items, max_per_source)
    window_used = PRIMARY_WINDOW_HOURS
    print(f"{PRIMARY_WINDOW_HOURS}h window: {len(selected)} eligible items", file=sys.stderr)

    if len(selected) < min_items:
        selected = select_for_window(deduped, FALLBACK_WINDOW_HOURS, now, keyword_gate, score_bonus_terms, max_items, max_per_source)
        window_used = FALLBACK_WINDOW_HOURS
        print(f"Below min-items, widened to {FALLBACK_WINDOW_HOURS}h window: {len(selected)} items", file=sys.stderr)

    if len(selected) < min_items:
        print(f"WARN: only {len(selected)} items after {FALLBACK_WINDOW_HOURS}h window (min-items={min_items})", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(selected, f, indent=2, ensure_ascii=False)

    print(f"\nWrote {len(selected)} items to {args.out} (window={window_used}h)", file=sys.stderr)
    by_bucket = {}
    for r in selected:
        by_bucket[r["bucket"]] = by_bucket.get(r["bucket"], 0) + 1
    for bucket, count in sorted(by_bucket.items()):
        print(f"  {bucket}: {count}", file=sys.stderr)
    named = sum(1 for r in selected if r["is_named_author"])
    print(f"  named_authors: {named}", file=sys.stderr)
    by_source = Counter(r["source"] for r in selected)
    for source, count in by_source.most_common():
        print(f"    {source}: {count}", file=sys.stderr)
    print(f"  score range: {min((r['score'] for r in selected), default=0)}-{max((r['score'] for r in selected), default=0)}", file=sys.stderr)

    if not args.no_update_seen:
        save_seen(args.seen_store, seen, [normalize_url(r["url"]) for r in selected])


if __name__ == "__main__":
    main()
