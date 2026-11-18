"""
Archive links posted in Slack channels to Markdown pages (for GitHub Pages / repo browsing).

Env vars:
  SLACK_BOT_TOKEN   xoxb-... token (scopes: channels:history, channels:read, users:read,
                    plus groups:history/groups:read if the channels are private)
  CHANNELS          e.g. "C0123PAPERS:papers,C0456DATA:resources"  (channel_id:page_name)
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

TOKEN = os.environ["SLACK_BOT_TOKEN"]
CHANNELS = dict(
    (cid.strip(), page.strip())
    for cid, page in (c.split(":", 1) for c in os.environ["CHANNELS"].split(",") if c.strip())
)
DATA = Path("data/archive.json")
DOCS = Path("docs")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Slack wraps links as <https://url|label> or <https://url>
SLACK_LINK = re.compile(r"<(https?://[^>|]+)(?:\|[^>]*)?>")
DOI = re.compile(r"\b(10\.\d{4,9}/[^\s?#]+)", re.I)
ARXIV = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.I)


def slack(method, **params):
    while True:
        r = requests.get(f"https://slack.com/api/{method}", headers=HEADERS, params=params, timeout=30)
        if r.status_code == 429:  # rate limited
            time.sleep(int(r.headers.get("Retry-After", 5)))
            continue
        j = r.json()
        if not j.get("ok"):
            raise RuntimeError(f"{method}: {j.get('error')}")
        return j


_users = {}
def user_name(uid):
    if uid and uid not in _users:
        try:
            u = slack("users.info", user=uid)["user"]
            _users[uid] = u.get("profile", {}).get("display_name") or u.get("real_name") or u.get("name") or uid
        except (RuntimeError, KeyError):
            _users[uid] = uid
    return _users.get(uid, "unknown")


def _et_al(names):
    return ", ".join(names[:3]) + (" et al." if len(names) > 3 else "")


def crossref_meta(doi):
    r = requests.get(f"https://api.crossref.org/works/{doi}", timeout=20)
    r.raise_for_status()
    w = r.json()["message"]
    return {
        "title": " ".join((w.get("title") or [""])[0].split()),
        "authors": _et_al([a.get("family", "") for a in w.get("author", [])]),
        "venue": (w.get("container-title") or [""])[0],
        "year": str((w.get("issued", {}).get("date-parts") or [[""]])[0][0] or ""),
    }


def arxiv_meta(arxiv_id):
    r = requests.get(f"https://export.arxiv.org/api/query?id_list={arxiv_id}", timeout=20)
    r.raise_for_status()
    entry = r.text.split("<entry>", 1)[1]  # IndexError if no entry -> caller falls back
    title = re.search(r"<title>(.*?)</title>", entry, re.S)
    year = re.search(r"<published>(\d{4})", entry)
    return {
        "title": " ".join(title.group(1).split()) if title else "",
        "authors": _et_al(re.findall(r"<name>(.*?)</name>", entry)),
        "venue": "arXiv",
        "year": year.group(1) if year else "",
    }


def page_title(url):
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    if t := re.search(r"<title[^>]*>(.*?)</title>", r.text[:200_000], re.S | re.I):
        return " ".join(html.unescape(t.group(1)).split())[:200]
    return ""


def enrich(url):
    """Best-effort metadata: Crossref for DOIs, arXiv API, else the page <title>.
    Each step falls through to the next if it fails; worst case the title is the URL."""
    lookups = []
    if m := DOI.search(url):
        lookups.append(lambda: crossref_meta(m.group(1)))
    if a := ARXIV.search(url):
        lookups.append(lambda: arxiv_meta(a.group(1)))
    lookups.append(lambda: {"title": page_title(url)})
    for lookup in lookups:
        try:
            meta = lookup()
            if meta.get("title"):
                return meta
        except Exception:
            pass
    return {"title": url}


def paginate(method, **params):
    msgs, cursor = [], None
    while True:
        j = slack(method, limit=200, **params, **({"cursor": cursor} if cursor else {}))
        msgs += j["messages"]
        cursor = j.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return msgs


def fetch_new(channel, since):
    """Yield (message, is_thread_reply) for everything posted after `since`,
    including new replies in older threads."""
    # Scan the whole visible window (90 days) so threads that get new replies
    # long after the parent message was posted are still caught.
    window_start = str(time.time() - 90 * 86400)
    for parent in paginate("conversations.history", channel=channel, oldest=window_start):
        if float(parent["ts"]) > float(since):
            yield parent, False
        if parent.get("reply_count") and float(parent.get("latest_reply", 0)) > float(since):
            for reply in paginate("conversations.replies", channel=channel, ts=parent["ts"])[1:]:
                if float(reply["ts"]) > float(since):
                    yield reply, True


def clean_text(text):
    text = SLACK_LINK.sub("", text)
    text = re.sub(r"<@(\w+)>", lambda m: "@" + user_name(m.group(1)), text)
    return " ".join(text.split())[:300]


def render(page, entries):
    rows = sorted(entries, key=lambda e: e["ts"], reverse=True)
    lines = [f"# {page.title()}\n", f"_Auto-generated from Slack. {len(rows)} entries. "
             f"Last updated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC._\n",
             "| Date | Title | Details | Shared by | Note |", "|---|---|---|---|---|"]
    for e in rows:
        details = " · ".join(x for x in (e.get("authors"), e.get("venue"), e.get("year")) if x)
        cell = lambda s: (s or "").replace("|", "\\|")
        lines.append(f"| {e['date']} | [{cell(e['title'])}]({e['url']}) | {cell(details)} "
                     f"| {cell(e['user'])} | {'↳ (thread) ' if e.get('in_thread') else ''}{cell(e['note'])} |")
    DOCS.mkdir(exist_ok=True)
    (DOCS / f"{page}.md").write_text("\n".join(lines) + "\n")


def render_index(counts):
    lines = ["# Research Link Archive\n", "Links shared in our Slack research channels.\n"]
    lines += [f"- [{page.title()}]({page}.md) ({n} entries)" for page, n in sorted(counts.items())]
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.md").write_text("\n".join(lines) + "\n")


def archive_channel(channel, page, since, db, seen):
    for msg, in_thread in fetch_new(channel, since):
        if msg.get("subtype") in ("channel_join", "bot_message"):
            continue
        # Messages without links (i.e. discussion) are skipped automatically.
        # Slack escapes &, <, > in message text, so unescape the URL.
        for url in dict.fromkeys(html.unescape(u) for u in SLACK_LINK.findall(msg.get("text", ""))):
            if (channel, url) in seen:
                continue
            seen.add((channel, url))
            db["entries"].append({
                "channel": channel, "page": page, "url": url, "ts": msg["ts"],
                "date": datetime.fromtimestamp(float(msg["ts"]), timezone.utc).strftime("%Y-%m-%d"),
                "user": user_name(msg.get("user")), "note": clean_text(msg.get("text", "")),
                "in_thread": in_thread,
                **enrich(url),
            })


def main():
    db = json.loads(DATA.read_text()) if DATA.exists() else {"last_run": "0", "entries": []}
    seen = {(e["channel"], e["url"]) for e in db["entries"]}
    run_started = str(time.time())

    failed = []
    for channel, page in CHANNELS.items():
        try:
            archive_channel(channel, page, db["last_run"], db, seen)
        except Exception as exc:  # e.g. not_in_channel, channel_not_found
            failed.append(channel)
            print(f"::error::channel {channel} ({page}): {exc}")

    # Only advance last_run if every channel succeeded; otherwise the next run
    # re-scans from the old last_run (duplicates are skipped via `seen`).
    if not failed:
        db["last_run"] = run_started

    pages = {e["page"] for e in db["entries"]} | set(CHANNELS.values())
    for page in pages:
        render(page, [e for e in db["entries"] if e["page"] == page])
    render_index({p: sum(e["page"] == p for e in db["entries"]) for p in pages})

    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(db, indent=1))
    print(f"{len(db['entries'])} total entries archived")
    if failed:
        sys.exit(1)  # makes the workflow run show as failed (GitHub emails on failure)


if __name__ == "__main__":
    main()