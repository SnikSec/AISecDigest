# AI Security Digest

Daily automated digest of AI-security news, emailed to you every morning. Pulls from security blogs, arXiv, GitHub Security Advisories, and a few named authors; filters and scores for relevance; has Claude (Haiku) summarize and re-score each item; sends only the high-value ones (score ≥ 80).

## Pipeline

Five stages, run in order by [`.github/workflows/dispatch.yml`](.github/workflows/dispatch.yml):

| Stage | Script | Does |
|---|---|---|
| 1 | [`ingest.py`](ingest.py) | Fetches every feed in [`sources.yaml`](sources.yaml) (RSS/Atom + a couple of WordPress `wp-json` sources whose RSS is disabled) plus GitHub Security Advisories filtered to ML/LLM packages. Normalizes everything to one record schema. |
| 2 | [`filter_rank.py`](filter_rank.py) | Dedupes, applies a 24h→48h recency window, gates by per-bucket keywords (named authors bypass the gate), scores deterministically (source tier + keyword density + bonus terms), and caps output at 15-25 candidates with per-bucket/per-source diversity guarantees. |
| 3 | [`summarize.py`](summarize.py) | Claude Haiku call per item: 2-sentence summary, bucket re-tag, 0-100 relevance re-score. |
| 4 | [`render.py`](render.py) | Renders a Markdown digest grouped by bucket, sorted by score, **filtered to score ≥ 80** (`--min-score` to change). |
| 5 | *(workflow step)* | Emails the digest via Resend, then commits the digest + updated `data/seen.json` back to the repo. |

## Buckets

- **ai_security** — general AI/LLM security: vulns, attacks, research
- **ai_identity** — non-human/workload/agent identity, auth, credentials
- **offsec_ai_workloads** — offensive security involving AI/agentic workloads

Some sources are pulled in regardless of topic match: see `named_authors` in `sources.yaml`.

## Automation

- Runs daily at **12:00 UTC** via cron in `dispatch.yml` (also triggerable manually: `gh workflow run dispatch.yml`).
- `data/seen.json` (a rolling 7-day list of already-sent item URLs) and the day's rendered digest get committed back to the repo after each run — this is how it avoids re-sending a story that's still inside the recency window on day 2.
- Intermediate files (`data/raw_items.json`, `data/filtered_items.json`, `data/summarized_items.json`) are regenerated every run and gitignored.

## Required repo secrets

| Secret | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | `summarize.py`'s Haiku calls |
| `RESEND_API_KEY` | Sending the digest email |
| `DIGEST_TARGET_EMAIL` | Recipient address — must exactly match the email your Resend account is registered under, since the sandbox sender (`onboarding@resend.dev`) only delivers to its own account owner |

## Required repo settings

**Settings → Actions → General → Workflow permissions → "Read and write permissions"** — needed so the workflow can commit `data/seen.json` and the digest back after each run.

## Running locally

```
pip install -r requirements.txt
python ingest.py          # -> data/raw_items.json
python filter_rank.py     # -> data/filtered_items.json (add --no-update-seen while testing)
ANTHROPIC_API_KEY=sk-... python summarize.py   # -> data/summarized_items.json (--dry-run skips the API)
python render.py          # -> digest/<date>.md
```

## Notes

- On a quiet day a bucket may render empty, or the whole digest may be short — that's intentional (the alternative is padding it with low-relevance filler).
- The min-score cutoff (80) lives in `render.py`; adjust `MIN_SCORE` or pass `--min-score` if it's too strict/loose after a few real days.
- Upgrading Resend beyond the sandbox sender (to email people other than the account owner) requires verifying a domain at resend.com/domains — not needed for personal use.
