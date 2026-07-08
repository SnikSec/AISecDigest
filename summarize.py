#!/usr/bin/env python3
"""Per-item Claude Haiku pass: 2-sentence summary, bucket tag, relevance re-score.

filter_rank.py's score is a deterministic heuristic (source tier + keyword
density); this stage adds an LLM judgment call on top - a human-readable
2-sentence summary, a bucket re-tag (catches heuristic mis-bucketing, e.g. a
GitHub Advisory that's actually more of an identity story than a generic
ai_security one), and a 0-100 relevance_score used as the final sort key in
render.py.

Requires ANTHROPIC_API_KEY in the environment. On a per-item API failure
(or in --dry-run mode), falls back to a truncated version of the original
summary, keeps the heuristic bucket, and reuses filter_rank's score - a
single flaky call should degrade that one item, not crash the whole daily
run.
"""
import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

MODEL = "claude-haiku-4-5-20251001"
MAX_WORKERS = 5
VALID_BUCKETS = ["ai_security", "ai_identity", "offsec_ai_workloads"]

TOOL_SCHEMA = {
    "name": "classify_and_summarize",
    "description": "Summarize and classify a news item for a daily AI-security digest read by security engineers.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Exactly two sentences summarizing why this item matters, written for a security practitioner audience. No preamble.",
            },
            "bucket": {
                "type": "string",
                "enum": VALID_BUCKETS,
                "description": (
                    "ai_security: general AI/LLM security (vulns, attacks, research). "
                    "ai_identity: non-human/workload/agent identity, auth, credentials. "
                    "offsec_ai_workloads: offensive security tooling/techniques involving AI/agentic workloads."
                ),
            },
            "relevance_score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "How relevant/important this item is for a daily AI-security digest read by security engineers, 0-100.",
            },
        },
        "required": ["summary", "bucket", "relevance_score"],
    },
}


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


ARXIV_BOILERPLATE = re.compile(r"^arXiv:\S+\s+Announce Type:\s*\S+\s*Abstract:\s*", re.IGNORECASE)


def naive_summary(record: dict) -> str:
    text = record.get("summary", "").strip()
    text = ARXIV_BOILERPLATE.sub("", text)
    if not text:
        return record["title"]
    sentences = text.replace("\n", " ").split(". ")
    fallback = ". ".join(sentences[:2]).strip()
    if fallback and not fallback.endswith("."):
        fallback += "."
    return fallback[:400] if fallback else record["title"]


def fallback_result(record: dict, reason: str) -> dict:
    return {
        **record,
        "filter_score": record["score"],
        "llm_summary": naive_summary(record),
        "score": record["score"],  # keep heuristic score as final sort key
        "summarize_fallback_reason": reason,
    }


def summarize_one(client, record: dict) -> dict:
    prompt = (
        f"Title: {record['title']}\n"
        f"Source: {record['source']}\n"
        f"Current bucket (heuristic, may be wrong): {record['bucket']}\n"
        f"Content:\n{record.get('summary', '')[:1500]}"
    )
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=300,
            tools=[TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "classify_and_summarize"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 - any API failure degrades this one item
        print(f"  WARN: API call failed for '{record['title'][:50]}': {exc}", file=sys.stderr)
        return fallback_result(record, str(exc))

    tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
    if tool_use is None:
        print(f"  WARN: no tool_use block for '{record['title'][:50]}'", file=sys.stderr)
        return fallback_result(record, "no tool_use block in response")

    data = tool_use.input
    bucket = data.get("bucket")
    if bucket not in VALID_BUCKETS:
        bucket = record["bucket"]

    return {
        **record,
        "filter_score": record["score"],
        "llm_summary": data.get("summary", naive_summary(record)),
        "bucket": bucket,
        "score": int(data.get("relevance_score", record["score"])),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_path", default="data/filtered_items.json", type=Path)
    parser.add_argument("--out", default="data/summarized_items.json", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Skip real API calls; use fallback summaries for every item")
    args = parser.parse_args()

    records = load_json(args.in_path)
    print(f"Loaded {len(records)} items", file=sys.stderr)

    if args.dry_run:
        print("DRY RUN: skipping Claude API calls, using fallback summaries", file=sys.stderr)
        results = [fallback_result(r, "dry-run") for r in records]
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ERROR: ANTHROPIC_API_KEY not set. Use --dry-run to test without an API key.", file=sys.stderr)
            sys.exit(1)
        import anthropic

        client = anthropic.Anthropic()
        results = [None] * len(records)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(summarize_one, client, r): i for i, r in enumerate(records)}
            for future in as_completed(futures):
                i = futures[future]
                results[i] = future.result()
                print(f"  [{i + 1}/{len(records)}] {records[i]['title'][:60]}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    fallbacks = sum(1 for r in results if "summarize_fallback_reason" in r)
    print(f"\nWrote {len(results)} items to {args.out} ({fallbacks} used fallback)", file=sys.stderr)
    by_bucket = {}
    for r in results:
        by_bucket[r["bucket"]] = by_bucket.get(r["bucket"], 0) + 1
    for bucket, count in sorted(by_bucket.items()):
        print(f"  {bucket}: {count}", file=sys.stderr)


if __name__ == "__main__":
    main()
