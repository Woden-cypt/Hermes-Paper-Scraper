#!/usr/bin/env python3
"""Offline tests for paper_bot (no network needed)."""
import datetime as dt
import json
import os
import tempfile

import paper_bot as pb

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# --- 1. Topic matching -----------------------------------------------------
check("match QKD phrase",
      "QKD" in pb.match_topics("Twin-field quantum key distribution over fiber", ""))
check("match acronym QKD with boundaries",
      "QKD" in pb.match_topics("A new QKD scheme", ""))
check("no false-positive on 'qkdx' substring",
      "QKD" not in pb.match_topics("The qkdxyz molecule", ""))
check("match optics topic",
      "Quantum optics / photonics" in pb.match_topics("Heralded single-photon source", ""))
check("unrelated paper matches nothing",
      pb.match_topics("Red cell distribution width in surgery", "") == [])
check("multi-topic paper",
      set(pb.match_topics("Device-independent quantum key distribution with entangled photons", ""))
      >= {"QKD", "Quantum cryptography", "Quantum optics / photonics"})

# --- 2. arXiv Atom parsing + date window -----------------------------------
ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2508.12345v1</id>
    <published>2026-08-24T17:00:00Z</published>
    <title>Decoy-state quantum key distribution with a chip source</title>
    <summary>We demonstrate a decoy state QKD system on an integrated chip.</summary>
    <author><name>A. Alice</name></author>
    <author><name>B. Bob</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2508.00001v2</id>
    <published>2026-08-02T09:00:00Z</published>
    <title>Old paper on single-photon detectors</title>
    <summary>An older heralded single-photon study.</summary>
    <author><name>C. Carol</name></author>
  </entry>
</feed>"""
cutoff = dt.date(2026, 8, 18)
parsed = pb.parse_arxiv_feed(ATOM, cutoff, query_topic="QKD")
check("arxiv: only in-window entry kept", len(parsed) == 1)
check("arxiv: correct id/url", parsed[0]["id"] == "arxiv:2508.12345"
      and parsed[0]["url"] == "https://arxiv.org/abs/2508.12345")
check("arxiv: version suffix stripped", parsed[0]["id"].endswith("2508.12345"))
check("arxiv: authors parsed", parsed[0]["authors"] == ["A. Alice", "B. Bob"])
check("arxiv: topic tagged QKD", "QKD" in parsed[0]["topics"])

# --- 3. Crossref parsing + strict filter -----------------------------------
CROSSREF = {"message": {"items": [
    {"DOI": "10.1038/s41566-026-0001-2",
     "title": ["Twin-field quantum key distribution over 600 km"],
     "container-title": ["Nature Photonics"],
     "created": {"date-parts": [[2026, 8, 21]]},
     "abstract": "<jats:p>We report twin-field QKD.</jats:p>",
     "URL": "https://doi.org/10.1038/s41566-026-0001-2",
     "author": [{"given": "D.", "family": "Delta"}]},
    {"DOI": "10.2147/idr.s604191",
     "title": ["Lymphopenia is associated with altered pathogen distribution"],
     "container-title": ["Infection and Drug Resistance"],
     "created": {"date-parts": [[2026, 8, 25]]},
     "abstract": "<jats:p>Clinical study.</jats:p>",
     "URL": "https://doi.org/10.2147/idr.s604191",
     "author": []},
]}}
cr = pb.parse_crossref_items(CROSSREF, "2026-08-18")
check("crossref: strict filter keeps only the QKD paper", len(cr) == 1)
check("crossref: journal captured in source", "Nature Photonics" in cr[0]["source"])
check("crossref: html stripped from abstract", "<jats:p>" not in cr[0]["abstract"])
check("crossref: date parsed", cr[0]["date"] == "2026-08-21")

# --- 4. Discord payload building + limits -----------------------------------
papers = parsed + cr
msgs = pb.build_discord_messages(papers, cutoff, dt.date(2026, 8, 25))
check("discord: first message is a header", "content" in msgs[0])
check("discord: embeds present", any("embeds" in m for m in msgs))
# validate limits and JSON-serializability
ok_limits = True
for m in msgs:
    json.dumps(m)  # must serialize
    for e in m.get("embeds", []):
        if len(e["title"]) > 256 or len(e["description"]) > 4096:
            ok_limits = False
    if len(m.get("embeds", [])) > 10:
        ok_limits = False
check("discord: within field + embed-count limits", ok_limits)

# long-title / long-abstract stress
big = {"id": "x", "source": "arXiv", "title": "T" * 500, "authors": ["N " * 50] * 8,
       "abstract": "A" * 5000, "url": "https://x", "date": "2026-08-24",
       "topics": ["QKD"]}
e = pb.build_embed(big)
check("discord: long title truncated to <=256", len(e["title"]) <= 256)
check("discord: long description truncated to <=4096", len(e["description"]) <= 4096)

# --- 5. Archive save/load + dedup ------------------------------------------
with tempfile.TemporaryDirectory() as d:
    orig = pb.ARCHIVE_PATH
    pb.ARCHIVE_PATH = os.path.join(d, "arch.json")
    try:
        pb.save_archive({p["id"]: p for p in papers})
        loaded = pb.load_archive()
        check("archive: roundtrip preserves count", len(loaded) == len(papers))
        check("archive: keyed by id", parsed[0]["id"] in loaded)
        # simulate dedup: a run that re-sees the same papers yields no new ones
        new = [p for p in papers if p["id"] not in loaded]
        check("archive: dedup removes already-seen", new == [])
    finally:
        pb.ARCHIVE_PATH = orig

# --- 6. arXiv query construction -------------------------------------------
q = pb.build_arxiv_query(["quantum key distribution", "BB84"])
check("query: has category clause", "cat:quant-ph" in q and "cat:physics.optics" in q)
check("query: has phrase clause", 'all:"quantum key distribution"' in q and 'all:"BB84"' in q)
check("query: joins categories with OR and phrases with OR",
      " OR " in q and " AND " in q)

print("\n" + ("ALL TESTS PASSED" if not FAILS else f"{len(FAILS)} TEST(S) FAILED: {FAILS}"))
raise SystemExit(1 if FAILS else 0)
