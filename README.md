# AI News Aggregator

A free, self-updating AI news page: pulls from a list of RSS feeds, groups
articles that are covering the *same* story, and shows a headline list —
each with a thumbnail image and a 3-line summary — you can tap to expand
into every source. No login, no server, no monthly cost.

## How it works

- `feeds.txt` — the list of RSS feeds to pull from (one URL per line).
  Curated and verified working as of 2026-07-28: a mix of AI-lab blogs,
  AI-specific sections of tech publications, independent commentary, a daily
  digest (TLDR AI), and two Hacker News search feeds (newest + best — see
  "Hot ranking" below). Only RSS/Atom sources — no HTML scraping (fragile,
  breaks on redesigns, higher maintenance than this project aims for); a
  source needs a feed to be included. Some well-known AI newsletters (e.g.
  The Rundown AI) don't publish one and so aren't in the list.
- `aggregate.py` — fetches every feed, groups same-story articles by comparing
  titles (word overlap + text similarity) within a rolling time window, and
  writes a static `docs/index.html`. For each story it also picks a thumbnail
  image (from the feed's media tags, an embedded `<img>` in the summary or
  full content, any other source covering the same story, or — as a last
  resort, bounded to `MAX_OG_FETCHES = 100` requests per run — the article
  page's `og:image`). That last-resort fetch tries a link-preview bot User-
  Agent (`WhatsApp/2.0`) before a regular browser one, since paywalled sites
  (confirmed on WSJ, which 401s a normal request) commonly allowlist
  link-preview bots so their og:image/description still show up when shared
  — exactly the metadata we want, nothing paywalled. And a 3-line summary —
  a real Gemini-written one if `GEMINI_API_KEY` is set
  (see below), otherwise a text-extracted excerpt from the feed's own
  description.
- `.github/workflows/build.yml` — a GitHub Action that runs the script
  hourly, 9am-8pm Berlin time, and publishes the result via **GitHub
  Pages** — completely free for a public repo.

## Hot ranking

Within each day, stories are ordered by a "hotness" score instead of pure
recency, using two free signals (no Google Trends / social-share API — those
either don't have a free tier or don't exist anymore for this use case):

- **Source count** — how many of your feeds picked up the same story. The
  strongest, most reliable signal we have for free.
- **Hacker News points/comments** — `hnrss.org/best` (added alongside the
  existing `/newest` query) returns already-popular HN discussions, so we
  get real point/comment counts instead of the near-zero counts a
  "newest"-only feed would have. Duplicate links between the two HN feeds
  are merged (`dedupe_by_link`) so the same post doesn't get double-counted
  as "2 sources".

Score = `source_count * 10 + log(1 + points) * 2 + log(1 + comments)` — the
log keeps a single viral HN post from completely dominating over stories
genuinely covered by several outlets. Stories with ≥15 HN points get a 🔥
badge. Any story with a badge (multi-source or hot) also gets a neon-green
border, since that's exactly the set of stories the hotness sort can pull
ahead of newer ones — a plain single-source, no-buzz story never jumps the
chronological queue, so it never needs the highlight. See `hotness_score()`
and the `story-hot` class in `aggregate.py`.

## Setup (10 minutes, no server needed)

1. Create a new **public** GitHub repo (e.g. `ai-news`), and push these files
   to it (`git init`, `git add .`, `git commit`, `git remote add origin ...`,
   `git push`).
2. In the repo, go to **Settings → Pages** and set:
   - Source: **Deploy from a branch**
   - Branch: `main`, folder: `/docs`
3. Go to **Settings → Actions → General → Workflow permissions** and select
   **Read and write permissions** (needed so the Action can commit the
   updated page back to the repo).
4. Go to the **Actions** tab and manually run "Build AI news page" once
   (or just wait — it also runs on every push, and every 2 hours after that).
5. Your page will be live at:
   `https://<your-username>.github.io/<repo-name>/`

That's it — from then on it updates itself every 2 hours for $0.

## Customizing

- **Add/remove feeds**: edit `feeds.txt`. Most news sites publish a feed at
  `<site>/feed` or `<site>/rss`. Feeds get pruned from here over time as
  sites move/block them (some, like Marktechpost, block the default Python
  user agent — that's why `aggregate.py` fetches with a browser-like one).
  Re-check periodically by running the script locally and watching stderr
  for `[warn] failed to fetch/parse ...`.
- **Off-topic articles from the HN feeds**: Hacker News's search matches a
  story's full text/URL, not just its title, so `q=AI+OR+LLM+...` pulls in
  some stories that only mention AI in passing (or a search false-positive).
  `AI_KEYWORDS_RE` in `aggregate.py` requires the *title* to contain an
  AI-related term before an `hnrss.org` entry is kept — edit that regex to
  tune what counts. It's only applied to the HN feeds; every other feed is
  an AI-dedicated blog/section already, so nothing there gets filtered.
- **How aggressively stories get grouped**: `--threshold` in `aggregate.py`
  (0–1, default 0.45). Lower = groups more loosely (fewer, broader stories);
  higher = only merges near-identical headlines.
- **How far back to look**: `--days` (default 4).
- **Run it locally** to test changes before pushing:
  ```
  pip install feedparser
  python3 aggregate.py --feeds feeds.txt --out docs/index.html
  open docs/index.html
  ```

## LLM summaries (optional)

Set a `GEMINI_API_KEY` repo secret (**Settings → Secrets and variables →
Actions**; get a free key at [Google AI Studio](https://aistudio.google.com/apikey))
and `aggregate.py` will ask Gemini (`gemini-flash-lite-latest` by default —
override with a `GEMINI_MODEL` env var) to write a real 2-3 sentence summary
for each story instead of using the text-extracted excerpt. It falls back
silently to the text-extracted summary on any failure — no key, quota limit,
network error, or blocked response — so the page always builds successfully
either way.

**Free-tier quota varies by exact model string, and by quota window** — on
one key we found the versioned model names (`gemini-2.5-flash-lite`,
`gemini-2.0-flash-lite`, `gemini-2.0-flash`) either blocked entirely or
capped at ~20 requests/**day**, while `gemini-flash-lite-latest` (the
default above — Google rolls `-latest` forward to newer model generations
over time) is capped at 15 requests/**minute** instead — a much friendlier
limit, since it resets every minute rather than for the rest of the day.
`aggregate.py` tells the two apart from the 429 body's `quotaId`
(`...PerDay...` vs `...PerMinute...`/`...PerHour...`): a per-day cap stops
the whole Gemini batch immediately (waiting won't help within one run), but
a per-minute cap waits out the suggested delay and retries — up to
`GEMINI_MAX_RATE_LIMIT_WAITS = 6` times — so most of the batch still gets a
real summary, just spread over a few minutes. Either way, whatever's left
over falls back to the text-extracted summary and the page still builds
fine. If you hit quota on your key too, check
`https://generativelanguage.googleapis.com/v1beta/models?key=YOUR_KEY` for
what's available, or just try another model via `GEMINI_MODEL`.

Note that a **consumer Gemini/Google AI Pro subscription does not raise
this** — we confirmed empirically that the same key still hits the free-tier
cap (error explicitly says `generate_content_free_tier_requests`) regardless
of any personal AI subscription tied to the Google account. Free-tier API
quota is a property of the **Google Cloud project** the key belongs to; to
remove the cap, enable Cloud Billing on that specific project (this model is
inexpensive per request) — a personal subscription and API billing are
separate systems.

Running locally without exporting `GEMINI_API_KEY` skips this step entirely
and behaves exactly as before.

## A note on the "grouping" approach

This groups stories using title/text similarity within a time window — it's
a simple, transparent method, not an LLM. It works well for near-duplicate
headlines about the same launch/event (which is most of what makes feeds feel
redundant). It won't catch stories that are worded very differently but
about the same underlying event. If you want LLM-based grouping too (better
recall, but calls the API more often), say the word and I'll swap the
clustering step for an embeddings-based one.
