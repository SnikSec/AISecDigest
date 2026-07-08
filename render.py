#!/usr/bin/env python3
"""Render the scored/summarized items into a Markdown digest, grouped by bucket.

Only items scoring >= --min-score (default 80, on summarize.py's 0-100
relevance scale) make it into the digest - the point is a short, high-value
read, not a dump of everything that survived filter_rank.py's broader
candidate pool. On a quiet day a bucket may end up empty; that's expected,
not a bug.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BUCKET_TITLES = {
    "ai_security": "AI Security",
    "ai_identity": "AI Identity & Non-Human Identity",
    "offsec_ai_workloads": "Offensive Security & AI Workloads",
}
BUCKET_ORDER = ["ai_security", "ai_identity", "offsec_ai_workloads"]
MIN_SCORE = 80


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def format_age(published_at: str | None, now: datetime) -> str:
    if not published_at:
        return ""
    try:
        published = datetime.fromisoformat(published_at)
    except ValueError:
        return ""
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    hours = (now - published).total_seconds() / 3600
    if hours < 1:
        return "<1h ago"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


def render_item(item: dict, now: datetime) -> str:
    title = item["title"].strip()
    url = item["url"]
    source = item["source"]
    age = format_age(item.get("published_at"), now)
    summary = item.get("llm_summary") or item.get("summary", "")[:400]
    score = item.get("score", 0)

    meta_parts = [source]
    if age:
        meta_parts.append(age)
    meta_parts.append(f"score {score}")
    if item.get("is_named_author"):
        meta_parts.append(f"★ {item['named_author']}")
    meta = " · ".join(meta_parts)

    lines = [f"### [{title}]({url})", f"*{meta}*", "", summary]
    return "\n".join(lines)


def render_bucket(bucket: str, kept: list[dict], total_before_filter: int, min_score: int, now: datetime) -> str:
    title = BUCKET_TITLES.get(bucket, bucket)

    if total_before_filter == 0:
        return f"## {title}\n\n_No items today._"

    if not kept:
        return (
            f"## {title}\n\n"
            f"_{total_before_filter} item(s) today, but none reached the score {min_score} bar._"
        )

    sorted_items = sorted(kept, key=lambda r: r["score"], reverse=True)
    body = "\n\n".join(render_item(item, now) for item in sorted_items)
    hidden = total_before_filter - len(kept)
    subtitle = f"\n\n*{hidden} lower-scoring item(s) hidden (min score {min_score}).*" if hidden else ""
    return f"## {title} ({len(kept)})\n\n{body}{subtitle}"


def render_digest(items: list[dict], min_score: int, now: datetime) -> str:
    by_bucket_all: dict[str, list[dict]] = {b: [] for b in BUCKET_ORDER}
    for item in items:
        by_bucket_all.setdefault(item["bucket"], []).append(item)

    kept_total = sum(1 for i in items if i.get("score", 0) >= min_score)
    kept_buckets = sum(
        1 for b in BUCKET_ORDER if any(i.get("score", 0) >= min_score for i in by_bucket_all.get(b, []))
    )

    date_str = now.strftime("%Y-%m-%d")
    header = (
        f"# AI Security Digest — {date_str}\n\n"
        f"{kept_total} items (of {len(items)} candidates) across {kept_buckets} buckets, min score {min_score}."
    )

    sections = []
    for b in BUCKET_ORDER:
        group = by_bucket_all.get(b, [])
        kept = [i for i in group if i.get("score", 0) >= min_score]
        sections.append(render_bucket(b, kept, len(group), min_score, now))

    # Any bucket not in the known display order (shouldn't normally happen) still gets shown.
    extra_buckets = [b for b in by_bucket_all if b not in BUCKET_ORDER]
    for b in extra_buckets:
        group = by_bucket_all[b]
        kept = [i for i in group if i.get("score", 0) >= min_score]
        sections.append(render_bucket(b, kept, len(group), min_score, now))

    return "\n\n---\n\n".join([header] + sections) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_path", default="data/summarized_items.json", type=Path)
    parser.add_argument("--out", default=None, type=Path, help="Defaults to digest/<date>.md")
    parser.add_argument("--min-score", type=int, default=MIN_SCORE, help="Only show items scoring at or above this (0-100)")
    args = parser.parse_args()

    items = load_json(args.in_path)
    now = datetime.now(timezone.utc)

    out_path = args.out or Path("digest") / f"{now.strftime('%Y-%m-%d')}.md"
    markdown = render_digest(items, args.min_score, now)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(markdown)

    kept_total = sum(1 for i in items if i.get("score", 0) >= args.min_score)
    print(f"Wrote digest to {out_path} ({kept_total}/{len(items)} items cleared min score {args.min_score})", file=sys.stderr)


if __name__ == "__main__":
    main()
