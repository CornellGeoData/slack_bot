"""
Archive links posted in Slack channels to Markdown pages (for GitHub Pages / repo browsing).

Env vars:
  SLACK_BOT_TOKEN   xoxb-... token (scopes: channels:history, channels:read, users:read,
                    plus groups:history/groups:read if the channels are private)
  CHANNELS          e.g. "C0123PAPERS:papers,C0456DATA:resources"  (channel_id:page_name)
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

TOKEN = os.environ["SLACK_BOT_TOKEN"]
CHANNELS = dict(c.split(":") for c in os.environ["CHANNELS"].split(","))
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
            _users[uid] = u["profile"].get("display_name") or u["real_name"]
        except RuntimeError:
            _users[uid] = uid
    return _users.get(uid, "unknown")


def enrich(url):
    """Best-effort metadata: Crossref for DOIs, arXiv API, else the page <title>."""
    meta = {"title": url}
    try:
        if m := DOI.search(url):
            w = requests.get(f"https://api.crossref.org/works/{m.group(1)}", timeout=20).json()["message"]
            authors = [a.get("family", "") for a in w.get("author", [])]
            meta = {
                "title": (w.get("title") or [url])[0],
                "authors": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
                "venue": (w.get("container-title") or [""])[0],
                "year": str((w.get("issued", {}).get("date-parts") or [[""]])[0][0]),
            }
        elif m := ARXIV.search(url):
            xml = requests.get(f"https://export.arxiv.org/api/query?id_list={m.group(1)}", timeout=20).text
            entry = xml.split("<entry>", 1)[-1]
            title = re.search(r"<title>(.*?)</title>", entry, re.S)
            names = re.findall(r"<name>(.*?)</name>", entry)
            year = re.search(r"<published>(\d{4})", entry)
            meta = {
                "title": " ".join(title.group(1).split()) if title else url,
                "authors": ", ".join(names[:3]) + (" et al." if len(names) > 3 else ""),
                "venue": "arXiv",
                "year": year.group(1) if year else "",
            }
        else:
            html = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).text[:200_000]
            if t := re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I):
                meta["title"] = " ".join(t.group(1).split())[:200]
    except Exception:
        pass
    return meta


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


def main():
    db = json.loads(DATA.read_text()) if DATA.exists() else {"last_run": "0", "entries": []}
    seen = {(e["channel"], e["url"]) for e in db["entries"]}
    run_started = str(time.time())

    for channel, page in CHANNELS.items():
        for msg, in_thread in fetch_new(channel, db["last_run"]):
            if msg.get("subtype") in ("channel_join", "bot_message"):
                continue
            # Messages without links (i.e. discussion) are skipped automatically.
            for url in dict.fromkeys(SLACK_LINK.findall(msg.get("text", ""))):
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
    db["last_run"] = run_started

    for page in set(CHANNELS.values()):
        render(page, [e for e in db["entries"] if e["page"] == page])

    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(db, indent=1))
    print(f"{len(db['entries'])} total entries archived")


if __name__ == "__main__":
    main()