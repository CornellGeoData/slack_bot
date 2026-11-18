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
from html.parser import HTMLParser
from itertools import groupby
from pathlib import Path
from urllib.parse import urlparse

import requests

TOKEN = os.environ["SLACK_BOT_TOKEN"]
def parse_channels(raw):
    """'C012ABC:papers,C034DEF:resources' -> {id: page}. Keeps only characters a Slack ID / page
    slug can contain, so stray quotes, spaces, or invisible characters picked up when copying
    the value (BOM, zero-width space) can't silently corrupt an ID."""
    out = {}
    for item in raw.split(","):
        if ":" not in item:
            continue
        cid, page = item.split(":", 1)
        cid, page = re.sub(r"[^A-Z0-9]", "", cid.upper()), re.sub(r"[^\w-]", "", page, flags=re.A)
        if cid and page:
            out[cid] = page
    return out


CHANNELS = parse_channels(os.environ["CHANNELS"])
DATA = Path("data/archive.json")
DOCS = Path("docs")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
UA = "slack-research-archiver/1.0"
# Bump when enrich() improves; entries with an older version get their metadata refreshed once.
META_VERSION = 2
DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true")

# Slack wraps links as <https://url|label> or <https://url>
SLACK_LINK = re.compile(r"<(https?://[^>|]+)(?:\|[^>]*)?>")
DOI = re.compile(r"\b(10\.\d{4,9}/[^\s?#]+)", re.I)
ARXIV = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.I)


def slack(method, **params):
    while True:
        r = requests.get(f"https://slack.com/api/{method}", headers=HEADERS, params=params, timeout=30)
        if r.status_code == 429:  # rate limited
            wait = int(r.headers.get("Retry-After", 5))
            print(f"  rate limited on {method}, retrying in {wait} s")
            time.sleep(wait)
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
    names = [n for n in names if n]
    return ", ".join(names[:3]) + (" et al." if len(names) > 3 else "")


def _family(name):
    """'Duffy, Kate' or 'Kate Duffy' -> 'Duffy' (matches the family names Crossref gives)."""
    name = _clean(name)
    return name.split(",")[0].strip() if "," in name else (name.split() or [""])[-1]


def _first(x):
    """CSL fields are sometimes a string, sometimes a list of strings."""
    return (x[0] if x else "") if isinstance(x, list) else (x or "")


def _clean(s):
    """Strip inline HTML (e.g. <i> in Crossref titles), unescape entities, collapse whitespace."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", s or "")).split())


def doi_meta(doi):
    """Resolve via doi.org content negotiation: works for Crossref (papers) and DataCite (datasets)."""
    r = requests.get(f"https://doi.org/{doi}", timeout=20,
                     headers={"Accept": "application/vnd.citationstyles.csl+json", "User-Agent": UA})
    r.raise_for_status()
    w = r.json()
    return {
        "title": _clean(_first(w.get("title"))),
        "authors": _et_al([a.get("family") or a.get("literal", "") for a in w.get("author", [])]),
        "venue": _clean(_first(w.get("container-title")) or w.get("publisher", "")),
        "year": str((w.get("issued", {}).get("date-parts") or [[""]])[0][0] or ""),
        "doi": doi,
    }


class _MetaParser(HTMLParser):
    """Collects <title> and <meta name/property=... content=...> tags (lists, since citation_author repeats)."""
    def __init__(self):
        super().__init__()
        self.meta, self.title, self._in_title = {}, "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and (key := a.get("name") or a.get("property")) and a.get("content"):
            self.meta.setdefault(key.lower(), []).append(a["content"])
        self._in_title = tag == "title" and not self.title

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


_arxiv_last = 0.0
def _arxiv_get(url, tries=4):
    """arXiv asks for >= 3 s between API calls and 429s shared IPs (like Actions runners) readily."""
    global _arxiv_last
    for attempt in range(tries):
        time.sleep(max(0, _arxiv_last + 3 - time.time()))
        _arxiv_last = time.time()
        r = requests.get(url, timeout=20, headers={"User-Agent": UA})
        if r.status_code not in (429, 503):
            break
        time.sleep(int(r.headers.get("Retry-After", 0)) or 5 * (attempt + 1))
    r.raise_for_status()
    return r


def arxiv_meta(arxiv_id):
    r = _arxiv_get(f"https://export.arxiv.org/api/query?id_list={arxiv_id}")
    entry = r.text.split("<entry>", 1)[1]  # IndexError if no entry -> caller falls back
    title = re.search(r"<title>(.*?)</title>", entry, re.S)
    year = re.search(r"<published>(\d{4})", entry)
    return {
        "title": " ".join(title.group(1).split()) if title else "",
        "authors": _et_al([_family(n) for n in re.findall(r"<name>(.*?)</name>", entry)]),
        "venue": "arXiv",
        "year": year.group(1) if year else "",
    }


def page_meta(url):
    """Scrape the page. Publishers almost all emit Google Scholar `citation_*` meta tags;
    if there's a DOI we resolve it properly, otherwise use the tags, else title + site name."""
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    p = _MetaParser()
    p.feed(r.text[:500_000])
    m = lambda k: _clean((p.meta.get(k) or [""])[0])

    doi = m("citation_doi") or m("dc.identifier") or m("prism.doi")
    if d := DOI.search(doi):
        try:
            return doi_meta(d.group(1).removeprefix("doi:"))
        except Exception:
            pass
    date = m("citation_publication_date") or m("citation_date") or m("citation_online_date")
    site = m("og:site_name") or urlparse(url).netloc.removeprefix("www.")
    return {
        "title": (m("citation_title") or m("og:title") or _clean(p.title))[:200],
        "authors": _et_al([_family(a) for a in p.meta.get("citation_author", [])]),
        "venue": m("citation_journal_title") or m("citation_conference_title") or site,
        "year": y.group(0) if (y := re.search(r"\b(19|20)\d\d\b", date)) else "",
    }


def enrich(url):
    """Best-effort metadata: DOI (Crossref/DataCite via doi.org), arXiv API, else the page itself.
    Each step falls through to the next if it fails; worst case the title is the URL."""
    lookups = []
    if m := DOI.search(url):
        lookups.append(lambda: doi_meta(m.group(1).rstrip(".,)")))
    if a := ARXIV.search(url):
        lookups.append(lambda: arxiv_meta(a.group(1)))
        # The abs page has citation_* tags; scraping a /pdf/ link would find no title at all.
        lookups.append(lambda: page_meta(f"https://arxiv.org/abs/{a.group(1)}"))
    lookups.append(lambda: page_meta(url))
    for lookup in lookups:
        try:
            meta = lookup()
            if meta.get("title"):
                return {**meta, "meta_version": META_VERSION}
        except Exception:
            pass
    return {"title": url, "venue": urlparse(url).netloc.removeprefix("www."), "meta_version": META_VERSION}


def paginate(method, **params):
    msgs, cursor = [], None
    while True:
        j = slack(method, limit=200, **params, **({"cursor": cursor} if cursor else {}))
        msgs += j["messages"]
        cursor = j.get("response_metadata", {}).get("next_cursor")
        if DEBUG and method == "conversations.history":
            print(f"  [debug] archive call {params}: {len(j['messages'])} messages, has_more={j.get('has_more')}, "
                  f"next_cursor={bool(cursor)}, warnings={j.get('response_metadata', {}).get('warnings') or j.get('warning')}")
        if not cursor:
            return msgs


def fetch_new(channel, since, stats):
    """Yield (message, is_thread_reply) for everything posted after `since`,
    including new replies in older threads."""
    # Scan the whole visible window (90 days) so threads that get new replies
    # long after the parent message was posted are still caught.
    window_start = str(time.time() - 90 * 86400)
    for parent in paginate("conversations.history", channel=channel, oldest=window_start):
        stats["in_window"] += 1
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


def _cell(s):
    """Make text safe inside a Markdown table cell / link label."""
    return (s or "").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;")


def _link(e):
    url = e["url"].replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("|", "%7C")
    details = " · ".join(x for x in (e.get("authors"), e.get("venue"), e.get("year")) if x)
    return f"[{_cell(e['title'])}]({url})" + (f"<br><sub>{_cell(details)}</sub>" if details else "")


def render(page, entries):
    # One row per Slack message: links shared together are listed in the same cell.
    by_msg = lambda e: (e["ts"], e["channel"])
    msgs = [list(g) for _, g in groupby(sorted(entries, key=by_msg, reverse=True), key=by_msg)]
    lines = [f"# {page.title()}\n", f"_Auto-generated from Slack. {len(entries)} links in {len(msgs)} messages. "
             f"Last updated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC._\n",
             "| Date | Link | Shared by | Note |", "|---|---|---|---|"]
    for group in msgs:
        e = group[0]
        links = "<br>".join(_link(x) for x in group)
        lines.append(f"| {e['date']} | {links} | {_cell(e['user'])} "
                     f"| {'↳ (thread) ' if e.get('in_thread') else ''}{_cell(e['note'])} |")
    DOCS.mkdir(exist_ok=True)
    (DOCS / f"{page}.md").write_text("\n".join(lines) + "\n")


def render_index(counts):
    lines = ["# Research Link Archive\n", "Links shared in our Slack research channels.\n"]
    lines += [f"- [{page.title()}]({page}.md) ({n} entries)" for page, n in sorted(counts.items())]
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.md").write_text("\n".join(lines) + "\n")


def _block_links(node):
    """Recursively find {"type": "link", "url": ...} elements in Block Kit rich_text blocks."""
    if isinstance(node, dict):
        if node.get("type") == "link" and node.get("url"):
            yield node["url"]
        for v in node.values():
            yield from _block_links(v)
    elif isinstance(node, list):
        for v in node:
            yield from _block_links(v)


def message_content(msg):
    """(text, urls) for a message. Forwarded/shared messages keep their content in
    `attachments` (with empty top-level text); app-posted messages may only have blocks."""
    texts = [msg.get("text", "")]
    texts += [a.get("text", "") for a in msg.get("attachments", []) if a.get("is_share") or a.get("is_msg_unfurl")]
    urls = [u for t in texts for u in SLACK_LINK.findall(t)] + list(_block_links(msg.get("blocks", [])))
    # Slack escapes &, <, > in message text, so unescape the URL.
    return " ".join(t for t in texts if t), list(dict.fromkeys(html.unescape(u) for u in urls))


def archive_channel(channel, page, since, db, seen):
    # in_window: top-level messages visible in the last 90 days; new: messages/replies after `since`
    stats = {"in_window": 0, "new": 0, "with_links": 0, "added": 0, "skipped_subtypes": {}, "file_only": 0}
    for msg, in_thread in fetch_new(channel, since, stats):
        stats["new"] += 1
        if (sub := msg.get("subtype")) in ("channel_join", "bot_message"):
            stats["skipped_subtypes"][sub] = stats["skipped_subtypes"].get(sub, 0) + 1
            continue
        text, urls = message_content(msg)
        if not urls and msg.get("files"):
            stats["file_only"] += 1  # uploaded files: nothing permanent to link to
        stats["with_links"] += bool(urls)
        # Messages without links (i.e. discussion) are skipped automatically.
        for url in urls:
            if (channel, url) in seen:
                continue
            seen.add((channel, url))
            stats["added"] += 1
            db["entries"].append({
                "channel": channel, "page": page, "url": url, "ts": msg["ts"],
                "date": datetime.fromtimestamp(float(msg["ts"]), timezone.utc).strftime("%Y-%m-%d"),
                "user": user_name(msg.get("user")), "note": clean_text(text),
                "in_thread": in_thread,
                **enrich(url),
            })
    return stats


def debug_channel(channel):
    """DEBUG=1: dump what Slack says about a channel, bypassing all of our filtering."""
    print(f"  [debug] raw CHANNELS = {os.environ['CHANNELS']!r}")
    print(f"  [debug] parsed id = {channel!r}")
    try:
        c = slack("conversations.info", channel=channel)["channel"]
        print("  [debug] info:", {k: c.get(k) for k in ("id", "name", "is_member", "is_private", "is_archived", "is_ext_shared")})
    except Exception as exc:
        print(f"  [debug] info failed: {exc}")
    # Which parameter makes the archive call differ from an unfiltered one?
    oldest = str(time.time() - 90 * 86400)
    for label, extra in [("limit=5", {"limit": 5}), ("limit=200", {"limit": 200}),
                         ("limit=5 + oldest", {"limit": 5, "oldest": oldest}),
                         ("limit=200 + oldest", {"limit": 200, "oldest": oldest})]:
        try:
            j = slack("conversations.history", channel=channel, **extra)
            print(f"  [debug] probe {label}: {len(j['messages'])} messages, has_more={j.get('has_more')}")
        except Exception as exc:
            print(f"  [debug] probe {label} failed: {exc}")
    try:
        j = slack("conversations.history", channel=channel, limit=5)  # no `oldest`: newest 5, any age
        print(f"  [debug] history: {len(j['messages'])} messages, has_more={j.get('has_more')}, "
              f"warnings={j.get('response_metadata', {}).get('warnings') or j.get('warning')}")
        for m in j["messages"]:
            age = (time.time() - float(m["ts"])) / 86400
            print(f"  [debug]   ts={m['ts']} ({age:.1f} days ago) subtype={m.get('subtype')} "
                  f"keys={sorted(m)} text={m.get('text', '')[:80]!r}")
    except Exception as exc:
        print(f"  [debug] history failed: {exc}")


def channel_name(channel):
    try:
        return "#" + slack("conversations.info", channel=channel)["channel"]["name"]
    except Exception:
        return "?"


def refresh_metadata(entries):
    """Re-run enrich() once on entries archived by an older enrich(). Only metadata fields
    change; a failed lookup never replaces a real title with the bare URL."""
    for e in entries:
        if e.get("meta_version", 1) >= META_VERSION:
            continue
        meta = enrich(e["url"])
        if meta["title"] == e["url"] and e.get("title") != e["url"]:
            meta.pop("title")
        e.update(meta)


def main():
    db = json.loads(DATA.read_text()) if DATA.exists() else {"last_run": "0", "entries": []}
    seen = {(e["channel"], e["url"]) for e in db["entries"]}
    run_started = str(time.time())

    # RESCAN=1 ignores last_run and re-reads the whole 90-day window (e.g. after improving
    # link detection). Safe: links already archived are skipped via `seen`.
    since = "0" if os.environ.get("RESCAN", "").lower() in ("1", "true") else db["last_run"]

    print(f"Scanning messages after {since} ({'RESCAN: full 90-day window' if since == '0' else 'since last_run'})")
    failed = []
    for channel, page in CHANNELS.items():
        try:
            stats = archive_channel(channel, page, since, db, seen)
            print(f"{channel} {channel_name(channel)} -> {page}: {stats}")
            if DEBUG:
                debug_channel(channel)
        except Exception as exc:  # e.g. not_in_channel, channel_not_found
            failed.append(channel)
            print(f"::error::channel {channel} ({page}): {exc}")

    refresh_metadata(db["entries"])

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