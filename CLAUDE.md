# Slack Research Archiver

A Slack bot for a student earth science / engineering research team. Our workspace is on Slack's free plan, which hides messages older than 90 days. The bot copies links shared in our research channels into a permanent archive in this repo, so the discussion stays in Slack and the links are kept for good.

## How it works

- `slack_archiver.py` runs once a day on GitHub Actions (`.github/workflows/archive.yml`). There is no server.
- It reads the channels listed in `CHANNELS`: parent messages with `conversations.history`, thread replies with `conversations.replies`.
- Each run looks back over the whole 90-day window, so new replies in old threads are still found. `last_run` in the data file decides what counts as new.
- Only messages that contain links are archived. Discussion that's only text is skipped. Duplicates are found by the pair `(channel, url)`.
- Metadata lookups: doi.org content negotiation for DOIs (covers Crossref papers and DataCite datasets), the arXiv API for arXiv IDs (rate-limited: 3 s spacing + retries), and otherwise the page's `citation_*` meta tags (or its DOI), falling back to `<title>` + site name. Bump `META_VERSION` when lookups improve; older entries get their metadata refreshed once.
- Links are read from message text, forwarded/shared message attachments, and Block Kit `link` elements.
- An entry's note is its message's text plus any **follow-up notes**: the same author's next messages within 5 minutes of the link (`FOLLOW_UP_WINDOW`), stopping at anyone else's message or the author's next link. Parts are joined with `\n` (rendered as `<br>`). The top level and each thread are separate streams. A link message is reprocessed when a follow-up to it is newer than `last_run`.
- Each run logs a per-channel summary (messages seen, with links, added, skipped subtypes). Run the workflow manually with **rescan** to ignore `last_run` and re-read the whole 90-day window.
- Output:
  - `data/archive.json` is the source of truth: every entry, plus `last_run`.
  - `docs/<page>.md` is a Markdown table for each channel, newest first, one row per Slack message (its links grouped in one cell), served by GitHub Pages from `docs/`. `docs/index.md` links the pages.
- The workflow commits changes to `data/` and `docs/` back to the repo.

## Configuration

- `SLACK_BOT_TOKEN` (repo secret): the `xoxb-` bot token.
- `CHANNELS` (repo variable): `CHANNEL_ID:page_name` pairs separated by commas, e.g. `C012ABC:papers,C034DEF:resources`.
- Bot token scopes: `channels:history`, `channels:read`, `users:read`. Private channels also need `groups:history` and `groups:read`.
- The bot has to be invited to every channel it reads (`/invite @AppName`).

## Running locally

```bash
pip install requests
export SLACK_BOT_TOKEN=xoxb-...
export CHANNELS="C012ABC:papers,C034DEF:resources"
python slack_archiver.py
```

Never commit tokens. Use a `.env` file and keep it in `.gitignore`.

## Constraints to keep in mind

- **The 90-day limit is permanent:** the API can't return messages older than 90 days on the free plan. Never delete or rebuild `data/archive.json` from scratch, because older entries can't be recovered.
- **Free plan app limit:** a free workspace can have at most 10 apps. New features should go into this same Slack app with the same token, not a new app.
- **Don't pass `oldest` to `conversations.history`:** in our workspace it returned no messages for one channel even though they were hours old. `history_since()` pages newest-first and stops at the 90-day cutoff itself.
- **Rate limits:** `slack()` retries when Slack returns HTTP 429. Keep all Slack calls going through it.
- **Data format:** if you change the fields in `archive.json`, make the code accept entries that don't have the new fields (use `e.get(...)`). Existing entries are never rebuilt.
- **Dependencies:** keep them light. Right now the only one is `requests`.

## Code conventions

- Python 3.12. The user knows Python at an intermediate-to-expert level.
- Prefer small, readable functions over frameworks.
- Make metadata lookups best-effort. If one fails, still archive the link with fallback info. Never crash the whole run over one link.

## Possible future work

- **Scheduled jobs** (weekly digest of new links, reminders): add new scripts and workflows that reuse `SLACK_BOT_TOKEN`.
- **Interactive features** (slash commands, replies when the bot is mentioned, emoji reactions): these need an always-running program. Use `slack_bolt` in Socket Mode, hosted on a free tier or a lab machine, inside the same Slack app.
- Possible extras: tags or categories (data source, model, paper topic), a searchable HTML page, BibTeX export for papers, and a "Discussion" link back to the Slack thread (the link only works for 90 days).