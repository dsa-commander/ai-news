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
import concurrent.futures
import datetime as dt
import html
import re
import sys
import urllib.request
from difflib import SequenceMatcher

import feedparser

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 12
IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
MAX_OG_FETCHES = 40  # bound extra per-article HTTP calls to keep runs fast
HN_BOILERPLATE_RE = re.compile(r"article url:.*points:\s*\d+.*#\s*comments:\s*\d+", re.IGNORECASE | re.DOTALL)

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


def three_line_summary(text, max_sentences=3, max_chars=280):
    """First few sentences of a summary, capped in length so it renders as
    roughly a 3-line snippet regardless of how CSS line-clamp handles it."""
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(sentences[:max_sentences]).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0] + "…"
    return out


def fetch_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def entry_image(entry, raw_html):
    """Best-effort image URL straight from the feed entry: media:content,
    media:thumbnail, an image enclosure, or the first <img> in the HTML body."""
    for media in entry.get("media_content", []) or []:
        if media.get("url") and (media.get("medium") in (None, "image") or "image" in (media.get("type") or "")):
            return media["url"]
    for thumb in entry.get("media_thumbnail", []) or []:
        if thumb.get("url"):
            return thumb["url"]
    for enc in entry.get("enclosures", []) or []:
        if enc.get("url") and "image" in (enc.get("type") or ""):
            return enc["url"]
    if raw_html:
        m = IMG_TAG_RE.search(raw_html)
        if m:
            return m.group(1)
    return None


def fetch_og_image(article_url):
    """Fallback: fetch the article page and pull its og:image/twitter:image
    meta tag. Only called for cluster leads that have no image from the feed."""
    try:
        req = urllib.request.Request(article_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=6) as resp:
            chunk = resp.read(65536).decode("utf-8", errors="ignore")
        m = OG_IMAGE_RE.search(chunk)
        return m.group(1) if m else None
    except Exception:
        return None


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
            raw = fetch_url(url)
        except Exception as e:
            print(f"[warn] failed to fetch {url}: {e}", file=sys.stderr)
            continue
        try:
            parsed = feedparser.parse(raw)
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
            raw_html = entry.get("summary", "") or entry.get("description", "")
            content_list = entry.get("content") or []
            raw_content = content_list[0].get("value", "") if content_list else ""
            summary = clean_html(raw_html)
            if HN_BOILERPLATE_RE.search(summary):
                summary = ""  # hnrss descriptions are just "Article URL / Points / Comments", not real content
            link = entry.get("link", "")
            image = entry_image(entry, raw_html or raw_content)
            articles.append({
                "title": title,
                "summary": summary[:400],
                "link": link,
                "source": source_name,
                "published": published,
                "image": image,
            })
    return articles


def attach_lead_images(clusters):
    """For each story's lead article, fill in a missing image by fetching
    the article page's og:image — bounded and run concurrently so it stays
    fast even across dozens of clusters."""
    leads_needing_fetch = [g[0] for g in clusters if not g[0].get("image")][:MAX_OG_FETCHES]
    if not leads_needing_fetch:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        future_to_lead = {ex.submit(fetch_og_image, lead["link"]): lead for lead in leads_needing_fetch if lead["link"]}
        for future in concurrent.futures.as_completed(future_to_lead):
            lead = future_to_lead[future]
            try:
                lead["image"] = future.result()
            except Exception:
                pass


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
    display: flex; gap: 12px; align-items: flex-start;
  }}
  .story summary::-webkit-details-marker {{ display: none; }}
  .story-thumb {{
    width: 84px; height: 84px; flex: 0 0 auto; border-radius: 8px;
    object-fit: cover; background: var(--border);
  }}
  .story-body {{ flex: 1 1 auto; min-width: 0; }}
  .story-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }}
  .story-title {{ font-size: 1rem; font-weight: 600; }}
  .story-meta {{ color: var(--muted); font-size: 0.78rem; white-space: nowrap; padding-left: 10px;}}
  .story-snippet {{
    color: var(--muted); font-size: 0.88rem; margin-top: 4px;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
    overflow: hidden;
  }}
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
    {thumb}
    <div class="story-body">
      <div class="story-head">
        <span class="story-title">{title}{badge}</span>
        <span class="story-meta">{when}</span>
      </div>
      {snippet}
    </div>
  </summary>
  <div class="sources">
    {source_items}
  </div>
</details>
"""

THUMB_TEMPLATE = '<img class="story-thumb" src="{src}" alt="" loading="lazy" onerror="this.remove()">'

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
        thumb = THUMB_TEMPLATE.format(src=html.escape(lead["image"])) if lead.get("image") else ""
        snippet_text = three_line_summary(lead["summary"])
        snippet = f'<div class="story-snippet">{html.escape(snippet_text)}</div>' if snippet_text else ""
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
            thumb=thumb,
            title=html.escape(lead["title"]),
            badge=badge,
            when=when,
            snippet=snippet,
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

    attach_lead_images(clusters)

    render(clusters, args.out)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
