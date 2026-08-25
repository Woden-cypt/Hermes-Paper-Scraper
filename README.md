# Quantum papers bot

A small bot that scans new papers each week and posts them to a Discord
channel. It looks at arXiv and at published journals (through Crossref), keeps
only the papers that match your topics, and saves everything it finds to a
JSON archive.

Topics it tracks out of the box:

- QKD (BB84, decoy-state, MDI, twin-field, CV-QKD, and so on)
- Quantum cryptography (device-independent, QRNG, secret sharing, signatures)
- Quantum optics and photonics (single photons, entangled pairs, SPDC, detectors)
- Quantum information and computing (networks, repeaters, memories, error correction)

No Python packages are needed. It uses the standard library only.

## What you get each week

The bot posts a short header ("12 new papers") followed by one card per paper.
Each card has the title as a clickable link, the authors, the matched topics,
the source, the date, and the start of the abstract. Every paper it sees is
also written to `papers_archive.json` so you have a growing, searchable record
and so the bot never posts the same paper twice.

## Setup

You need two things: a Discord webhook, and a place to run the bot once a week.
The easiest free option is GitHub Actions. Setup takes about ten minutes.

### 1. Create a Discord webhook

1. In Discord, make (or pick) a server and a channel for the digest.
2. Open the channel settings, go to Integrations, then Webhooks.
3. Click New Webhook, give it a name like "Papers bot", and copy the webhook URL.

Keep that URL private. Anyone who has it can post to your channel.

### 2. Test it locally first (optional but recommended)

If you have Python on your machine, you can preview a digest with no Discord
and no setup:

```bash
python paper_bot.py --dry-run
```

That prints the papers it would post. To do a real post from your machine:

```bash
export DISCORD_WEBHOOK_URL="paste-your-webhook-url-here"
python paper_bot.py
```

### 3. Run it weekly with GitHub Actions (recommended)

1. Create a new GitHub repository (private is fine) and push these files to it.
2. In the repo, open Settings, then Secrets and variables, then Actions.
3. Click New repository secret. Name it `DISCORD_WEBHOOK_URL` and paste your
   webhook URL as the value.
4. That is it. The workflow in `.github/workflows/weekly.yml` runs every Monday
   morning. It posts to Discord and commits the updated archive back to the repo.

To test the automation right away, open the Actions tab, pick "Weekly quantum
papers digest", and click Run workflow. The first run may be larger than usual
because the archive starts empty. It is capped at 60 papers per run.

The schedule is set in UTC. The default is `0 6 * * 1`, which is Monday at
06:00 UTC. That is about 08:00 in Paris in summer and 07:00 in winter. Edit the
`cron` line in the workflow file to change it.

## Tuning

Open `paper_bot.py` and edit the `CONFIG` block near the top.

- `TOPICS` is where you add or remove topics and keywords. A paper matches a
  topic if any of that topic's phrases appears in the title or abstract. Keep
  phrases specific to avoid noise.
- `ARXIV_CATEGORIES` limits the arXiv search. `quant-ph` and `physics.optics`
  are set by default.
- `USE_ARXIV` and `USE_CROSSREF` turn each source on or off. If Crossref feels
  noisy, set `USE_CROSSREF = False` and rely on arXiv alone.
- `MAX_PAPERS_IN_DIGEST` caps how many papers go into one post.
- `CONTACT_EMAIL` is sent to the APIs as a courtesy. Put your real email there.

## Command line options

```
python paper_bot.py --dry-run     # print the digest, do not post, do not touch the archive
python paper_bot.py --days 14      # look back 14 days instead of 7
python paper_bot.py --all          # ignore the archive, report every match in the window
python paper_bot.py                # real run, posts to Discord (needs the webhook)
```

A dry run never writes the archive, so you can preview as many times as you
like and your first real run will still post everything it finds.

## If it says "no new matches"

The bot only posts papers it has not seen before. If a real run says there are
no new matches when you expected some, the archive already has them. To post
the full week anyway, ignore the archive for one run:

```
python paper_bot.py --all
```

Or start the archive fresh by deleting it, then run normally:

```
rm papers_archive.json
python paper_bot.py
```

## Running on your own machine instead of GitHub

If you would rather run it on your Mac, you can use a scheduled job.

With cron, run `crontab -e` and add a line like this (Monday at 08:00):

```
0 8 * * 1  DISCORD_WEBHOOK_URL="your-url" /usr/bin/python3 /full/path/to/paper_bot.py
```

The archive file will grow next to the script.

## How it works, in short

1. For each topic, it asks arXiv for recent papers in your categories that
   match the topic phrases, and asks Crossref for recent journal articles that
   match, ranked by relevance.
2. It keeps only papers inside the date window whose title or abstract really
   contains one of your phrases.
3. It removes papers it has already seen, using the archive.
4. It posts what is left to Discord and updates the archive.

## Notes

- arXiv and Crossref are free and need no API key.
- Sorting Crossref by date alone returns a lot of unrelated work, so the bot
  uses relevance ranking plus a strict keyword check on the title and abstract.
- If a source is down during a run, the bot logs the error and continues with
  the other source rather than failing.
