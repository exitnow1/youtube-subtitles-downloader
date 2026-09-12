---
name: yt-subs-downloader
description: "Use when downloading YouTube subtitles in bulk, checking channel uploads for new videos, retrying failed subtitle jobs, scheduling subtitle work, or querying subtitle status and dashboard data. Trigger on requests like '자막 받아줘', '채널 새 영상 자막', '쇼츠 자막', 'youtube subtitles batch', 'subtitle retry', '자막 목록 보여줘'."
---

# YT Subs Downloader

## Objective

Batch-download YouTube subtitles (video files never fetched) with channel/playlist/single modes, scan snapshots, retry queue, scheduler integration, and SQLite-backed status tracking.

## When to use

- One or more video URLs → subtitle files
- Channel or playlist URL → many subtitle files, optionally filtered
- "What's new / what's missing subtitles" questions about a channel
- Failed subtitle jobs to retry, immediately or on a schedule
- Shorts-only, long-only, or mixed subtitle collection

## Setup

App lives in `$APP` (default `C:\Users\rpt53\Desktop\Programs and applications\Able\개발\Youtube downloader` — adjust if moved):

```powershell
$py = "$APP\yt_dlp_subtitle_downloader.py"
python $py --help   # full flag list, always authoritative
```

Requires `pip install yt-dlp browser_cookie3`. First run creates `subtitle_config.json` next to the script.

## Workflow

1. **Parse the request**: URLs, mode, subtitle langs, filters (date/duration/range/type), output dir. Ask only if the target URL is absent.
2. **Run the CLI** (never parallelize; one invocation at a time).
3. **Read results** from stdout plus `SUMMARY_JSON:` (with `--json`): per-item outcomes and counts. Never silently drop failures — report them with reasons.
4. **Report to the user**: saved count, skipped/cached counts, failures with reasons, dashboard path.

## CLI reference

| Option | Meaning |
| --- | --- |
| `--channel URL` | Channel subtitles |
| `--playlist URL` | Playlist subtitles |
| `--single URL [URL...]` | One or more individual videos |
| `--list URL` | DB-backed video list with status marks, no API calls (scan first) |
| `--missing URL` | Download missing-subtitles only (requires `--yes`) |
| `--retry-failed [SEL]` | Retry failed jobs (`all`, `1,3`, `2-5`) |
| `--scan URL` | Snapshot listing only, no downloads |
| `--dashboard` | (Re)build `dashboard.html` |
| `--type long\|shorts\|both` | Video kind filter (default long) |
| `--langs ko,en\|all` | Subtitle languages (default ko,en) |
| `--no-auto` | Exclude auto-generated captions |
| `--format vtt\|srt` | Subtitle format (default vtt) |
| `--encoding utf-8\|bom\|cp949` | File encoding (default utf-8) |
| `--out DIR` | Output folder override |
| `--date ">= 2024-01-01"` | Upload-date condition (`==,!=,<,<=,>,>=`) |
| `--dur 5-30` | Duration range in minutes |
| `--range 1-50` | Item number range |
| `--keyword TEXT` | Title filter (list mode) |
| `--status all\|downloaded\|missing` | Status filter (list mode) |
| `--order asc\|desc` | Missing-download order |
| `--select SEL` | Missing-download selection |
| `--retry-select SEL` | Retry selection |
| `--yes` | Start without confirmation (missing mode) |
| `--json` | Append machine-readable `SUMMARY_JSON:` line |
| `--headless` | Skip popup notifications |

```powershell
python $py --channel "https://www.youtube.com/@handle" --type both --date ">= 2024-01-01" --json
python $py --single "URL1" "URL2" --langs ko --out "D:\subs"
python $py --missing "https://www.youtube.com/@handle" --order desc --select "1-10" --yes --json
python $py --retry-failed --json
python $py --scan "https://www.youtube.com/@handle" --scan-type both
```

## Safety contract (do not violate)

- Sequential requests only — the app paces itself (sleeps, jitter, backoff). Never run two instances at once.
- The app's **circuit breaker** aborts after repeated 429s; do not immediately rerun a blocked batch. Report stage + advice: 429 → wait ~30+ min or schedule off-hours; 403 → refresh cookies (close browser, rerun).
- `--missing` without `--yes` refuses to start by design; pass `--yes` only with explicit user approval.
- Every attempt is recorded in `subtitle_jobs.db` (success capped at 3 per URL+langs). Surface DB/log paths when users ask about history.

## Limits

- Private/member-only videos need a logged-in browser cookie; failures say why.
- Listings carry no upload dates (YouTube API limit) — date filters apply at download time.
- Playlist items are classified long/shorts by URL and duration heuristic; channel tabs are exact.
- SRT headers use `#` comments most players ignore; strict players may warn.
- Scheduled runs need the PC on; app must stay installed at the registered path.

## Common mistakes

- Running parallel instances to "go faster" → triggers blocks; always sequential.
- Retrying 429 failures immediately → respect the backoff, suggest scheduling.
- Expecting `--list` to fetch fresh data → it reads the DB; run `--scan` first.
- Forgetting `--yes` on `--missing` → it intentionally does nothing; ask the user.
