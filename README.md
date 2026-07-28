# AI News Aggregator

A free, self-updating AI news page: pulls from a list of RSS feeds, groups
articles that are covering the *same* story, and shows a short headline list
you can tap to expand into every source + snippet. No login, no server,
no monthly cost.

## How it works

- `feeds.txt` — the list of RSS feeds to pull from (one URL per line).
- `aggregate.py` — fetches every feed, groups same-story articles by comparing
  titles (word overlap + text similarity) within a rolling time window, and
  writes a static `docs/index.html`.
- `.github/workflows/build.yml` — a GitHub Action that runs the script every
  2 hours and publishes the result via **GitHub Pages** — completely free
  for a public repo.

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
  `<site>/feed` or `<site>/rss`. I've seeded it with common AI news sources —
  double-check each one still resolves, since sites occasionally move feeds.
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

## A note on the "grouping" approach

This groups stories using title/text similarity within a time window — it's
a simple, transparent method, not an LLM. It works well for near-duplicate
headlines about the same launch/event (which is most of what makes feeds feel
redundant). It won't catch stories that are worded very differently but
about the same underlying event. If you want LLM-based grouping (better
recall, but requires an API key and has a small ongoing cost per run), say
the word and I'll swap the clustering step for an embeddings-based one.
