#!/usr/bin/env python3
"""
Quantum papers bot.

Scans recent papers from arXiv and Crossref for topics you care about,
then posts a digest to a Discord channel (via a webhook) and stores what
it found in a JSON archive.

No third-party packages required. Python 3.8+ standard library only.

Usage:
    # Test without posting (prints the digest to your screen):
    python paper_bot.py --dry-run

    # Real run (needs the DISCORD_WEBHOOK_URL environment variable):
    python paper_bot.py

    # Look back a different number of days (default 7):
    python paper_bot.py --days 14 --dry-run

Author's note: edit the CONFIG block below to change topics or keywords.
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# --------------------------------------------------------------------------
# CONFIG  -  edit this block to tune the bot
# --------------------------------------------------------------------------

# A polite contact address sent to the APIs. Put your real email here.
CONTACT_EMAIL = "tos.rimprongern@gmail.com"

# arXiv categories to search within. quant-ph and physics.optics cover your field.
ARXIV_CATEGORIES = ["quant-ph", "physics.optics"]

# Topics. Each topic has a label and a list of key phrases.
# A paper matches a topic if ANY of its phrases appears in the title or abstract.
# Phrases are matched case-insensitively. Keep them specific to cut noise.
TOPICS = {
    "QKD": [
        "quantum key distribution", "QKD", "BB84", "decoy state", "decoy-state",
        "measurement-device-independent", "MDI-QKD", "twin-field", "twin field",
        "coherent one-way", "continuous-variable quantum key", "CV-QKD",
    ],
    "Quantum cryptography": [
        "quantum cryptography", "quantum secure", "device-independent",
        "quantum random number", "quantum secret sharing", "quantum money",
        "position-based quantum", "quantum digital signature",
    ],
    "Quantum optics / photonics": [
        "single photon", "single-photon", "entangled photon", "photon pair",
        "photon-pair", "integrated photonics", "quantum light", "squeezed light",
        "heralded", "SPDC", "spontaneous parametric down-conversion",
        "superconducting nanowire", "quantum optics",
    ],
    "Quantum information / computing": [
        "quantum information", "quantum computing", "quantum computer",
        "quantum network", "quantum repeater", "quantum internet",
        "quantum error correction", "quantum memory", "quantum teleportation",
        "quantum entanglement distribution",
    ],
}

# Which sources to use.
USE_ARXIV = True
USE_CROSSREF = True

# How many days back to look (can be overridden with --days).
DEFAULT_DAYS_BACK = 7

# Safety caps so one run never floods the channel or the APIs.
MAX_PER_ARXIV_QUERY = 120     # max results fetched per topic query from arXiv
MAX_PER_CROSSREF_QUERY = 60   # max results fetched per topic query from Crossref
MAX_PAPERS_IN_DIGEST = 60     # hard cap on how many papers get posted per run

# File where every paper ever seen is stored (for your records + de-duplication).
ARCHIVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "papers_archive.json")

# --------------------------------------------------------------------------
# End of CONFIG
# --------------------------------------------------------------------------

USER_AGENT = f"quantum-paper-bot/1.0 (mailto:{CONTACT_EMAIL})"
ARXIV_API = "http://export.arxiv.org/api/query"
CROSSREF_API = "https://api.crossref.org/works"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def http_get(url, retries=3, timeout=40):
    """GET a URL with a friendly User-Agent and simple retry."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 - network is messy, keep going
            last_err = e
            log(f"  request failed (attempt {attempt}/{retries}): {e}")
            time.sleep(3 * attempt)
    raise last_err


def normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def match_topics(title, abstract):
    """Return the list of topic labels whose phrases appear in title/abstract."""
    blob = f"{title} \n {abstract}".lower()
    matched = []
    for topic, phrases in TOPICS.items():
        for phrase in phrases:
            p = phrase.lower()
            # word-ish boundary check for short all-caps acronyms to avoid junk
            if len(p) <= 4:
                if re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", blob):
                    matched.append(topic)
                    break
            elif p in blob:
                matched.append(topic)
                break
    return matched


# --------------------------------------------------------------------------
# arXiv
# --------------------------------------------------------------------------

def build_arxiv_query(phrases):
    cat_clause = " OR ".join(f"cat:{c}" for c in ARXIV_CATEGORIES)
    phrase_clause = " OR ".join(f'all:"{p}"' for p in phrases)
    return f"({cat_clause}) AND ({phrase_clause})"


def parse_arxiv_feed(raw, cutoff, query_topic=None):
    """Parse an arXiv Atom feed string into a list of paper dicts within the window."""
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)
    papers = []
    for entry in root.findall("atom:entry", ns):
        published = entry.findtext("atom:published", default="", namespaces=ns)
        try:
            pub_dt = dt.datetime.strptime(published[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if pub_dt < cutoff:
            continue

        arxiv_url = entry.findtext("atom:id", default="", namespaces=ns).strip()
        arxiv_id = arxiv_url.rsplit("/", 1)[-1]
        title = normalize(entry.findtext("atom:title", default="", namespaces=ns))
        abstract = normalize(entry.findtext("atom:summary", default="", namespaces=ns))
        authors = [normalize(a.findtext("atom:name", default="", namespaces=ns))
                   for a in entry.findall("atom:author", ns)]

        topics = match_topics(title, abstract)
        if not topics and query_topic:
            topics = [query_topic]  # matched via the query even if phrase split oddly
        if not topics:
            continue

        key = re.sub(r"v\d+$", "", arxiv_id)  # drop version suffix for dedup
        papers.append({
            "_key": key,
            "id": f"arxiv:{key}",
            "source": "arXiv",
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "url": f"https://arxiv.org/abs/{key}",
            "date": pub_dt.isoformat(),
            "topics": topics,
        })
    return papers


def fetch_arxiv(cutoff):
    """Fetch recent arXiv papers per topic, filter to the cutoff date."""
    found = {}
    for topic, phrases in TOPICS.items():
        search = build_arxiv_query(phrases)
        params = {
            "search_query": search,
            "start": 0,
            "max_results": MAX_PER_ARXIV_QUERY,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
        log(f"arXiv: querying topic '{topic}' ...")
        try:
            raw = http_get(url)
            batch = parse_arxiv_feed(raw, cutoff, query_topic=topic)
        except Exception as e:  # noqa: BLE001
            log(f"  arXiv query failed for '{topic}': {e}")
            time.sleep(3)
            continue

        for p in batch:
            key = p["_key"]
            if key in found:
                for t in p["topics"]:
                    if t not in found[key]["topics"]:
                        found[key]["topics"].append(t)
            else:
                found[key] = p
        time.sleep(3)  # be polite to arXiv between topic queries
    for p in found.values():
        p.pop("_key", None)
    log(f"arXiv: {len(found)} papers in window.")
    return list(found.values())


# --------------------------------------------------------------------------
# Crossref (published journal articles)
# --------------------------------------------------------------------------

def parse_crossref_items(data, fallback_date):
    """Parse a Crossref JSON response into paper dicts, strict-filtered by topic phrase."""
    papers = []
    for item in data.get("message", {}).get("items", []):
        title_list = item.get("title") or []
        title = normalize(title_list[0]) if title_list else ""
        if not title:
            continue
        abstract = normalize(re.sub(r"<[^>]+>", "", item.get("abstract", "")))

        # Strict filter: keep only if a topic phrase actually appears in title/abstract.
        topics = match_topics(title, abstract)
        if not topics:
            continue

        doi = (item.get("DOI") or "").lower()
        key = f"doi:{doi}" if doi else f"title:{title.lower()[:80]}"

        created = item.get("created", {}).get("date-parts", [[None]])[0]
        try:
            date_str = dt.date(created[0], created[1], created[2]).isoformat()
        except (TypeError, IndexError, ValueError):
            date_str = fallback_date

        authors = []
        for a in item.get("author", []) or []:
            name = " ".join(x for x in [a.get("given"), a.get("family")] if x)
            if name:
                authors.append(name)

        journal = ""
        ct = item.get("container-title") or []
        if ct:
            journal = normalize(ct[0])

        papers.append({
            "id": key,
            "source": f"Crossref ({journal})" if journal else "Crossref",
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "url": item.get("URL", f"https://doi.org/{doi}" if doi else ""),
            "date": date_str,
            "topics": topics,
        })
    return papers


def fetch_crossref(cutoff):
    """Fetch recently published journal articles per topic from Crossref.

    Uses Crossref's default relevance ranking (sorting by date alone returns
    far too much unrelated work), constrained to the recent window, then keeps
    only items whose title or abstract actually contains a topic phrase.
    """
    found = {}
    from_date = cutoff.isoformat()
    for topic, phrases in TOPICS.items():
        # Use the two or three most descriptive phrases as the search query.
        query_terms = " ".join(phrases[:3])
        params = {
            "query.bibliographic": query_terms,
            "filter": f"from-created-date:{from_date},type:journal-article",
            "rows": MAX_PER_CROSSREF_QUERY,
            "select": "DOI,title,author,container-title,created,abstract,URL",
            "mailto": CONTACT_EMAIL,
        }
        url = f"{CROSSREF_API}?{urllib.parse.urlencode(params)}"
        log(f"Crossref: querying topic '{topic}' ...")
        try:
            raw = http_get(url)
            data = json.loads(raw)
            batch = parse_crossref_items(data, from_date)
        except Exception as e:  # noqa: BLE001
            log(f"  Crossref query failed for '{topic}': {e}")
            time.sleep(2)
            continue

        for p in batch:
            key = p["id"]
            if key in found:
                for t in p["topics"]:
                    if t not in found[key]["topics"]:
                        found[key]["topics"].append(t)
            else:
                found[key] = p
        time.sleep(2)
    log(f"Crossref: {len(found)} papers in window.")
    return list(found.values())


# --------------------------------------------------------------------------
# Archive (storage + de-duplication across runs)
# --------------------------------------------------------------------------

def load_archive():
    if not os.path.exists(ARCHIVE_PATH):
        return {}
    try:
        with open(ARCHIVE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {p["id"]: p for p in data.get("papers", [])}
    except Exception as e:  # noqa: BLE001
        log(f"Could not read archive ({e}); starting fresh.")
        return {}


def save_archive(archive):
    payload = {
        "updated": dt.datetime.utcnow().isoformat() + "Z",
        "count": len(archive),
        "papers": sorted(archive.values(), key=lambda p: p.get("date", ""), reverse=True),
    }
    with open(ARCHIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# Digest formatting + Discord posting
# --------------------------------------------------------------------------

def truncate(text, n):
    text = text or ""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def format_text_digest(papers, cutoff, today):
    lines = []
    lines.append(f"Quantum papers digest  |  {cutoff.isoformat()} to {today.isoformat()}")
    lines.append(f"{len(papers)} new paper(s)\n")
    for i, p in enumerate(papers, 1):
        authors = ", ".join(p["authors"][:4])
        if len(p["authors"]) > 4:
            authors += ", et al."
        lines.append(f"{i}. {p['title']}")
        lines.append(f"   {authors}")
        lines.append(f"   [{', '.join(p['topics'])}]  {p['source']}  {p['date']}")
        lines.append(f"   {truncate(p['abstract'], 300)}")
        lines.append(f"   {p['url']}\n")
    return "\n".join(lines)


COLOR_BY_TOPIC = {
    "QKD": 0x2E86DE,
    "Quantum cryptography": 0x8E44AD,
    "Quantum optics / photonics": 0xE67E22,
    "Quantum information / computing": 0x16A085,
}


def build_embed(paper):
    """Build one Discord embed dict for a paper (kept within Discord field limits)."""
    authors = ", ".join(paper["authors"][:4])
    if len(paper["authors"]) > 4:
        authors += ", et al."
    first_topic = paper["topics"][0] if paper["topics"] else ""
    desc = ""
    if authors:
        desc += f"*{truncate(authors, 200)}*\n"
    desc += f"`{' | '.join(paper['topics'])}`  ·  {paper['source']}  ·  {paper['date']}\n\n"
    desc += truncate(paper["abstract"], 600)
    return {
        "title": truncate(paper["title"], 250),        # Discord title limit is 256
        "url": paper["url"],
        "description": truncate(desc, 4000),            # Discord description limit is 4096
        "color": COLOR_BY_TOPIC.get(first_topic, 0x3498DB),
    }


def build_discord_messages(papers, cutoff, today):
    """Return the list of webhook payloads (header + embed batches) to send."""
    messages = [{
        "content": (
            f"**Weekly quantum papers digest**\n"
            f"{cutoff.isoformat()} to {today.isoformat()}  |  "
            f"**{len(papers)}** new paper(s)"
        )
    }]
    batch = []
    for p in papers:
        batch.append(build_embed(p))
        if len(batch) == 5:  # up to 10 allowed; 5 keeps each message readable
            messages.append({"embeds": batch})
            batch = []
    if batch:
        messages.append({"embeds": batch})
    return messages


def post_to_discord(webhook_url, papers, cutoff, today):
    """Post the digest to Discord as a header message plus embed batches."""
    for msg in build_discord_messages(papers, cutoff, today):
        _discord_send(webhook_url, msg)
        time.sleep(1)


def _discord_send(webhook_url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status in (200, 204):
                    return
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited
                retry = 2 * attempt
                log(f"  Discord rate limited, waiting {retry}s ...")
                time.sleep(retry)
                continue
            log(f"  Discord post failed: HTTP {e.code} {e.reason}")
            return
        except Exception as e:  # noqa: BLE001
            log(f"  Discord post error (attempt {attempt}): {e}")
            time.sleep(2 * attempt)
    log("  Discord post gave up after retries.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Quantum papers bot")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS_BACK,
                        help="How many days back to look (default 7).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the digest instead of posting to Discord.")
    parser.add_argument("--all", action="store_true",
                        help="Ignore the archive and report every match in the window.")
    args = parser.parse_args()

    today = dt.date.today()
    cutoff = today - dt.timedelta(days=args.days)
    log(f"Looking for papers from {cutoff} to {today}\n")

    papers = []
    if USE_ARXIV:
        papers += fetch_arxiv(cutoff)
    if USE_CROSSREF:
        papers += fetch_crossref(cutoff)

    # Merge duplicates that came from more than one source (by title).
    merged = {}
    for p in papers:
        tkey = re.sub(r"[^a-z0-9]", "", p["title"].lower())[:60]
        if tkey in merged:
            for t in p["topics"]:
                if t not in merged[tkey]["topics"]:
                    merged[tkey]["topics"].append(t)
        else:
            merged[tkey] = p
    papers = list(merged.values())

    # De-duplicate against the archive so you only get NEW papers each run.
    archive = load_archive()
    if not args.all:
        fresh = [p for p in papers if p["id"] not in archive]
    else:
        fresh = papers

    # Sort newest first, then cap.
    fresh.sort(key=lambda p: p["date"], reverse=True)
    if len(fresh) > MAX_PAPERS_IN_DIGEST:
        log(f"Capping digest at {MAX_PAPERS_IN_DIGEST} (found {len(fresh)}).")
        fresh = fresh[:MAX_PAPERS_IN_DIGEST]

    log(f"\n{len(fresh)} new paper(s) to report.\n")

    # Dry run is a preview only. It must NOT write the archive, otherwise a
    # later real run would treat these same papers as already seen.
    if args.dry_run:
        if fresh:
            print(format_text_digest(fresh, cutoff, today))
        else:
            print(f"No new papers for {cutoff.isoformat()} to {today.isoformat()}.")
        return

    # From here on it is a real run, so a webhook is required. Check it BEFORE
    # touching the archive, so we never mark papers as seen without posting them.
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        log("ERROR: DISCORD_WEBHOOK_URL is not set. Use --dry-run to preview, "
            "or set the webhook to post.")
        sys.exit(1)

    if not fresh:
        log("Nothing new. Sending a short 'quiet week' note.")
        _discord_send(webhook, {"content":
            f"Weekly quantum papers digest: no new matches for "
            f"{cutoff.isoformat()} to {today.isoformat()}."})
    else:
        post_to_discord(webhook, fresh, cutoff, today)
        log("Posted to Discord.")

    # Update the archive only after a successful real run.
    for p in papers:
        archive[p["id"]] = p
    save_archive(archive)


if __name__ == "__main__":
    main()
