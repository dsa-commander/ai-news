#!/usr/bin/env python3
"""
AI News Aggregator
-------------------
Fetches a list of RSS/Atom feeds, groups articles that are covering the same
underlying story ("same article, different source"), and renders a single
static HTML page: a short list of stories, click to expand and see every
source + snippet.

Free to run forever: this script + a GitHub Actions cron job + GitHub Pages
is $0/month, no server, no login.

Usage:
    python3 aggregate.py --feeds feeds.txt --out docs/index.html --days 4
"""
import argparse
import datetime as dt
import html
import re
import sys
from difflib import SequenceMatcher

import feedparser

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "at", "by", "from", "as", "its", "it",
    "new", "how", "why", "what", "this", "that", "will", "has", "have",
    "says", "say", "after", "over", "into", "up", "out", "vs", "v",
}

WORD_RE = re.compile(r"[a-z0-9']+")


def clean_html(raw):
    """Strip tags from a feed summary and collapse whitespace."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(title):
    words = WORD_RE.findall(title.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def title_similarity(a, b):
    """Blend of word-overlap (Jaccard) and character similarity."""
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        jaccard = 0.0
    else:
        jaccard = len(ta & tb) / len(ta | tb)
    char_ratio = SequenceMatcher(None, a.lower(), b.lower()).ratio()
    return 0.7 * jaccard + 0.3 * char_ratio


def fetch_articles(feed_urls, since):
    articles = []
    for url in feed_urls:
        url = url.strip()
        if not url or url.startswith("#"):
            continue
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"[warn] failed to parse {url}: {e}", file=sys.stderr)
            continue
        source_name = parsed.feed.get("title", url)
        for entry in parsed.entries:
            published = None
            for key in ("published_parsed", "updated_parsed"):
                if entry.get(key):
                    published = dt.datetime(*entry[key][:6], tzinfo=dt.timezone.utc)
                    break
            if published is None:
                published = dt.datetime.now(dt.timezone.utc)
            if published < since:
                continue
            title = entry.get("title", "(untitled)").strip()
            summary = clean_html(entry.get("summary", "") or entry.get("description", ""))
            link = entry.get("link", "")
            articles.append({
                "title": title,
                "summary": summary[:400],
                "link": link,
                "source": source_name,
                "published": published,
            })
    return articles


def cluster_articles(articles, threshold=0.45, window_hours=72):
    """Union-find style clustering: same story if titles are similar and
    published within `window_hours` of each other."""
    articles = sorted(articles, key=lambda a: a["published"], reverse=True)
    clusters = []  # list of lists of article-indices
    parent = list(range(len(articles)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(len(articles)):
        for j in range(i + 1, len(articles)):
            dt_diff = abs((articles[i]["published"] - articles[j]["published"]).total_seconds())
            if dt_diff > window_hours * 3600:
                continue
            sim = title_similarity(articles[i]["title"], articles[j]["title"])
            if sim >= threshold:
                union(i, j)

    groups = {}
    for i in range(len(articles)):
        r = find(i)
        groups.setdefault(r, []).append(articles[i])

    clustered = list(groups.values())
    # newest story first
    clustered.sort(key=lambda g: max(a["published"] for a in g), reverse=True)
    for g in clustered:
        g.sort(key=lambda a: a["published"], reverse=True)
    return clustered


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI News Aggregator</title>
<style>
  :root {{
    --bg: #0f1115; --card: #171a21; --text: #e8e9ec; --muted: #9aa1ac;
    --accent: #7cc4ff; --border: #262b35;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    line-height: 1.45;
  }}
  header {{ padding: 28px 20px 8px; max-width: 760px; margin: 0 auto; }}
  header h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
  header p {{ color: var(--muted); margin: 0; font-size: 0.9rem; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 12px 20px 60px; }}
  .story {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; margin-bottom: 12px; overflow: hidden;
  }}
  .story summary {{
    cursor: pointer; list-style: none; padding: 14px 16px;
    display: flex; gap: 10px; align-items: baseline; justify-content: space-between;
  }}
  .story summary::-webkit-details-marker {{ display: none; }}
  .story-title {{ font-size: 1rem; font-weight: 600; }}
  .story-meta {{ color: var(--muted); font-size: 0.78rem; white-space: nowrap; padding-left: 10px;}}
  .story-snippet {{ color: var(--muted); font-size: 0.88rem; padding: 0 16px 14px; margin-top: -6px; }}
  .sources {{ border-top: 1px solid var(--border); padding: 6px 16px 14px; }}
  .source-item {{ padding: 10px 0; border-bottom: 1px solid var(--border); }}
  .source-item:last-child {{ border-bottom: none; }}
  .source-name {{ color: var(--accent); font-size: 0.78rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.02em;}}
  .source-item a {{ color: var(--text); text-decoration: none; font-weight: 500; }}
  .source-item a:hover {{ text-decoration: underline; }}
  .source-summary {{ color: var(--muted); font-size: 0.85rem; margin-top: 2px; }}
  .badge {{
    display: inline-block; background: #22303f; color: var(--accent);
    font-size: 0.72rem; padding: 2px 8px; border-radius: 999px; margin-left: 8px;
  }}
  .updated {{ color: var(--muted); font-size: 0.78rem; text-align: center; padding: 20px 0; }}
</style>
</head>
<body>
<header>
  <h1>🧠 AI News</h1>
  <p>Same-story articles are grouped together. Tap a headline to see every source.</p>
</header>
<main>
{stories}
<div class="updated">Last updated {updated}</div>
</main>
</body>
</html>
"""

STORY_TEMPLATE = """<details class="story">
  <summary>
    <span class="story-title">{title}{badge}</span>
    <span class="story-meta">{when}</span>
  </summary>
  <div class="story-snippet">{snippet}</div>
  <div class="sources">
    {source_items}
  </div>
</details>
"""

SOURCE_ITEM_TEMPLATE = """<div class="source-item">
  <div class="source-name">{source}</div>
  <a href="{link}" target="_blank" rel="noopener">{title}</a>
  <div class="source-summary">{summary}</div>
</div>
"""


def render(clusters, out_path):
    story_html = []
    for group in clusters:
        lead = group[0]
        badge = f'<span class="badge">{len(group)} sources</span>' if len(group) > 1 else ""
        when = lead["published"].strftime("%b %d, %H:%M UTC")
        source_items = "".join(
            SOURCE_ITEM_TEMPLATE.format(
                source=html.escape(a["source"]),
                link=html.escape(a["link"]),
                title=html.escape(a["title"]),
                summary=html.escape(a["summary"]),
            )
            for a in group
        )
        story_html.append(STORY_TEMPLATE.format(
            title=html.escape(lead["title"]),
            badge=badge,
            when=when,
            snippet=html.escape(lead["summary"][:220]),
            source_items=source_items,
        ))
    page = PAGE_TEMPLATE.format(
        stories="\n".join(story_html) if story_html else "<p>No articles found in this window.</p>",
        updated=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feeds", default="feeds.txt")
    ap.add_argument("--out", default="docs/index.html")
    ap.add_argument("--days", type=int, default=4, help="only include articles from the last N days")
    ap.add_argument("--threshold", type=float, default=0.45, help="title-similarity threshold to merge (0-1)")
    args = ap.parse_args()

    with open(args.feeds, encoding="utf-8") as f:
        feed_urls = [l for l in f.read().splitlines() if l.strip() and not l.strip().startswith("#")]

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    print(f"Fetching {len(feed_urls)} feeds...")
    articles = fetch_articles(feed_urls, since)
    print(f"Got {len(articles)} articles from the last {args.days} days.")

    clusters = cluster_articles(articles, threshold=args.threshold)
    print(f"Grouped into {len(clusters)} stories.")

    render(clusters, args.out)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
