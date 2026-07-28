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
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

import feedparser

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 12

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
GEMINI_TIMEOUT = 20
MAX_GEMINI_CALLS = 60  # bound API usage per run (free-tier friendly)
IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
OG_DESCRIPTION_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
MAX_OG_FETCHES = 40  # bound extra per-article HTTP calls to keep runs fast
HN_BOILERPLATE_RE = re.compile(r"article url:.*points:\s*\d+.*#\s*comments:\s*\d+", re.IGNORECASE | re.DOTALL)

# Generic "no photo" icon shown when a story has no usable image.
_PLACEHOLDER_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 84 84'>"
    "<rect width='84' height='84' rx='8' fill='#20242e'/>"
    "<circle cx='30' cy='32' r='7' fill='#3a4150'/>"
    "<path d='M14 62 L34 40 L48 54 L58 44 L70 62 Z' fill='#3a4150'/>"
    "</svg>"
)
PLACEHOLDER_THUMB = "data:image/svg+xml," + urllib.parse.quote(_PLACEHOLDER_SVG)

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "at", "by", "from", "as", "its", "it",
    "new", "how", "why", "what", "this", "that", "will", "has", "have",
    "says", "say", "after", "over", "into", "up", "out", "vs", "v",
}

WORD_RE = re.compile(r"[a-z0-9']+")


def clean_html(raw):
    """Strip tags from a feed summary and collapse whitespace. Unescapes
    entities first — some feeds double-encode (e.g. "&amp;lt;small&amp;gt;"),
    which would otherwise decode into literal tags *after* stripping."""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
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


def looks_like_caption(text):
    """Heuristic for feeds (e.g. Google's blog) that put the hero image's alt
    text in <description> instead of real content: real prose has sentence
    punctuation, a bare image caption usually doesn't."""
    return bool(text) and len(text) < 220 and not re.search(r"[.!?]", text)


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


def fetch_og_meta(article_url):
    """Fallback: fetch the article page and pull its og:image/twitter:image
    and og:description meta tags. Only called for cluster leads missing an
    image and/or a usable summary from the feed itself."""
    try:
        req = urllib.request.Request(article_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=6) as resp:
            chunk = resp.read(65536).decode("utf-8", errors="ignore")
    except Exception:
        return {}
    meta = {}
    m = OG_IMAGE_RE.search(chunk)
    if m:
        meta["image"] = html.unescape(m.group(1))
    m = OG_DESCRIPTION_RE.search(chunk)
    if m:
        meta["summary"] = clean_html(m.group(1))
    return meta


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
            elif looks_like_caption(summary):
                summary = ""  # e.g. Google's blog RSS puts the hero image's alt text here
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


def augment_leads(clusters):
    """For each story's lead article, fill in a missing image and/or summary
    by fetching the article page's og:image/og:description — bounded and run
    concurrently so it stays fast even across dozens of clusters."""
    leads_needing_fetch = [
        g[0] for g in clusters if not g[0].get("image") or not g[0].get("summary")
    ][:MAX_OG_FETCHES]
    if not leads_needing_fetch:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        future_to_lead = {ex.submit(fetch_og_meta, lead["link"]): lead for lead in leads_needing_fetch if lead["link"]}
        for future in concurrent.futures.as_completed(future_to_lead):
            lead = future_to_lead[future]
            try:
                meta = future.result()
            except Exception:
                continue
            if not lead.get("image") and meta.get("image"):
                lead["image"] = meta["image"]
            if not lead.get("summary") and meta.get("summary") and not looks_like_caption(meta["summary"]):
                lead["summary"] = meta["summary"][:400]


class GeminiQuotaExhausted(Exception):
    """Raised on a 429 so the caller can stop the whole batch instead of
    burning through every remaining call for the same failure (Gemini's free
    tier quota for a model is commonly a fixed per-day cap, not a per-minute
    one — retrying doesn't help within a single run)."""


def gemini_summarize(title, source, excerpt, link):
    """Ask Gemini for a tight 2-3 sentence news-card summary. Returns None on
    a recoverable, single-request failure (network error, blocked response)
    so the caller falls back to the text-extracted summary. Raises
    GeminiQuotaExhausted on a 429."""
    prompt = (
        "Write a concise, factual 2-3 sentence summary (about 40-50 words "
        "total) of this news story, suitable for a headline card. Plain "
        "prose only — no markdown, no bullet points, no preamble like "
        "'Here is a summary', don't editorialize.\n\n"
        f"Headline: {title}\n"
        f"Source: {source}\n"
        f"Excerpt: {excerpt[:1500] if excerpt else '(none — infer only from the headline)'}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 200},
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    try:
        req = urllib.request.Request(GEMINI_URL, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
            payload = json.loads(resp.read())
        text_out = payload["candidates"][0]["content"]["parts"][0]["text"]
        return clean_html(text_out) or None
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise GeminiQuotaExhausted(str(e)) from e
        print(f"[warn] Gemini summarize failed ({e.code}) for {link}: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[warn] Gemini summarize failed for {link}: {e}", file=sys.stderr)
        return None


def gemini_summarize_leads(clusters):
    """Replace each story's summary with a real LLM-written one via Gemini,
    when GEMINI_API_KEY is configured. Stops the whole batch as soon as the
    API reports quota exhaustion (the free tier can be as low as ~20
    requests/day for a given model) instead of wasting calls on guaranteed
    failures. No key set (e.g. running locally without exporting it)
    silently keeps the existing text-extracted summaries."""
    if not GEMINI_API_KEY:
        return
    leads = [g[0] for g in clusters][:MAX_GEMINI_CALLS]
    if not leads:
        return
    print(f"Summarizing up to {len(leads)} stories with Gemini ({GEMINI_MODEL})...")
    done = 0
    for lead in leads:
        try:
            result = gemini_summarize(lead["title"], lead["source"], lead["summary"], lead["link"])
        except GeminiQuotaExhausted as e:
            print(f"[warn] Gemini quota exhausted after {done} summaries, using text-extracted "
                  f"summaries for the rest: {e}", file=sys.stderr)
            break
        if result:
            lead["summary"] = result
            done += 1
    if done:
        print(f"Gemini summarized {done}/{len(leads)} stories.")


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
  .story summary, a.story-link {{
    cursor: pointer; list-style: none; padding: 14px 16px;
    display: flex; gap: 12px; align-items: flex-start;
    text-decoration: none; color: inherit;
  }}
  .story summary::-webkit-details-marker {{ display: none; }}
  a.story-link:hover .story-title {{ text-decoration: underline; }}
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
  .day-heading {{
    font-size: 1rem; font-weight: 700; margin: 22px 4px 10px; color: var(--text);
  }}
  main > *:first-child .day-heading {{ margin-top: 0; }}
  details.day-collapsed {{ margin-bottom: 12px; }}
  details.day-collapsed > summary.day-heading {{
    cursor: pointer; list-style: none; display: flex; align-items: center; gap: 8px;
    margin: 22px 0 0; padding: 12px 16px; background: var(--card);
    border: 1px solid var(--border); border-radius: 10px;
  }}
  details.day-collapsed > summary.day-heading::-webkit-details-marker {{ display: none; }}
  details.day-collapsed[open] > summary.day-heading {{ border-radius: 10px 10px 0 0; margin-bottom: 10px; }}
  .day-stories {{ padding-top: 2px; }}
  .updated {{ color: var(--muted); font-size: 0.78rem; text-align: center; padding: 20px 0; }}
</style>
</head>
<body>
<header>
  <h1>🧠 AI News</h1>
  <p>Same-story articles are grouped together — tap to see every source. Single-source stories link straight to the article.</p>
</header>
<main>
{stories}
<div class="updated">Last updated {updated}</div>
</main>
</body>
</html>
"""

DAY_OPEN_TEMPLATE = """<section class="day-section">
  <h2 class="day-heading">{label}</h2>
  {stories}
</section>
"""

DAY_COLLAPSED_TEMPLATE = """<details class="day-section day-collapsed">
  <summary class="day-heading">{label} <span class="badge">{count} stories</span></summary>
  <div class="day-stories">
    {stories}
  </div>
</details>
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

SINGLE_STORY_TEMPLATE = """<a class="story story-link" href="{link}" target="_blank" rel="noopener">
  {thumb}
  <div class="story-body">
    <div class="story-head">
      <span class="story-title">{title}</span>
      <span class="story-meta">{when}</span>
    </div>
    {snippet}
  </div>
</a>
"""

THUMB_TEMPLATE = (
    '<img class="story-thumb" src="{src}" alt="" loading="lazy" '
    "onerror=\"this.onerror=null;this.src='" + PLACEHOLDER_THUMB + "'\">"
)

SOURCE_ITEM_TEMPLATE = """<div class="source-item">
  <div class="source-name">{source}</div>
  <a href="{link}" target="_blank" rel="noopener">{title}</a>
  <div class="source-summary">{summary}</div>
</div>
"""


def render_story(group):
    lead = group[0]
    when = lead["published"].strftime("%H:%M UTC")
    image_src = lead.get("image") or PLACEHOLDER_THUMB
    thumb = THUMB_TEMPLATE.format(src=html.escape(image_src))
    snippet_text = three_line_summary(lead["summary"])
    snippet = f'<div class="story-snippet">{html.escape(snippet_text)}</div>' if snippet_text else ""

    if len(group) == 1:
        # Only one source for this story — link straight to the article
        # instead of expanding into a redundant one-item source list.
        return SINGLE_STORY_TEMPLATE.format(
            link=html.escape(lead["link"]),
            thumb=thumb,
            title=html.escape(lead["title"]),
            when=when,
            snippet=snippet,
        )

    badge = f'<span class="badge">{len(group)} sources</span>'
    source_items = "".join(
        SOURCE_ITEM_TEMPLATE.format(
            source=html.escape(a["source"]),
            link=html.escape(a["link"]),
            title=html.escape(a["title"]),
            summary=html.escape(a["summary"]),
        )
        for a in group
    )
    return STORY_TEMPLATE.format(
        thumb=thumb,
        title=html.escape(lead["title"]),
        badge=badge,
        when=when,
        snippet=snippet,
        source_items=source_items,
    )


def group_by_day(clusters):
    """Bucket stories by the UTC calendar date of their lead article,
    newest day first."""
    groups = {}
    for group in clusters:
        day = group[0]["published"].date()
        groups.setdefault(day, []).append(group)
    return sorted(groups.items(), key=lambda kv: kv[0], reverse=True)


def render(clusters, out_path):
    today = dt.datetime.now(dt.timezone.utc).date()
    section_html = []
    for day, day_clusters in group_by_day(clusters):
        stories = "".join(render_story(g) for g in day_clusters)
        if day == today:
            label = f"Today — {day.strftime('%b %d, %Y')}"
            section_html.append(DAY_OPEN_TEMPLATE.format(label=label, stories=stories))
        else:
            if day == today - dt.timedelta(days=1):
                label = f"Yesterday — {day.strftime('%b %d, %Y')}"
            else:
                label = day.strftime("%A, %b %d, %Y")
            section_html.append(DAY_COLLAPSED_TEMPLATE.format(
                label=label, count=len(day_clusters), stories=stories,
            ))
    page = PAGE_TEMPLATE.format(
        stories="\n".join(section_html) if section_html else "<p>No articles found in this window.</p>",
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

    augment_leads(clusters)
    gemini_summarize_leads(clusters)

    render(clusters, args.out)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
