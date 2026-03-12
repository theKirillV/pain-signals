#!/usr/bin/env python3
"""
Reddit Problem Discovery — scrappy, zero-dependency scanner.

Reads a subreddit's recent posts + comments via Reddit's public .json endpoints,
scores them for pain signals, and outputs a ranked plain-text report.

Usage:
    python discover.py                        # defaults to r/shopify, hot+new
    python discover.py --subreddit ecommerce  # different sub
    python discover.py --sort top --limit 50  # top posts, more results
    python discover.py --deep                 # also fetch comments for top posts
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# HTTP (adapted from last30days-skill, stdlib only)
# ---------------------------------------------------------------------------

USER_AGENT = "problem-discovery/0.1 (research script)"
MAX_RETRIES = 3
RETRY_DELAY = 2.0


def http_get(url: str, timeout: int = 30, retries: int = MAX_RETRIES) -> Any:
    """GET JSON from a URL with retry + backoff."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers)

    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if 400 <= e.code < 500 and e.code != 429:
                raise
            delay = RETRY_DELAY * (2 ** attempt)
            if e.code == 429:
                ra = e.headers.get("Retry-After")
                delay = float(ra) if ra and ra.isdigit() else delay + 1
            sys.stderr.write(f"  [retry {attempt+1}] HTTP {e.code}, waiting {delay:.0f}s\n")
            time.sleep(delay)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
    raise last_err


def reddit_json(path: str, params: Optional[Dict[str, str]] = None) -> Any:
    """Fetch Reddit .json endpoint."""
    path = path.rstrip("/")
    if not path.endswith(".json"):
        path += ".json"
    qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"https://www.reddit.com{path}?raw_json=1" + (f"&{qs}" if qs else "")
    return http_get(url)


# ---------------------------------------------------------------------------
# Pain signal scoring
# ---------------------------------------------------------------------------

# Words/phrases that signal someone is experiencing a problem
PAIN_WORDS = [
    "help", "issue", "problem", "broken", "bug", "error", "crash", "fail",
    "frustrat", "annoy", "disappoint", "terrible", "horrible", "worst",
    "can't", "cannot", "won't", "doesn't work", "not working", "stopped working",
    "anyone else", "am i the only", "is it just me",
    "struggling", "stuck", "confused", "lost", "desperate", "urgent",
    "scam", "rip off", "waste of time", "waste of money",
    "support", "no response", "unreliable", "inconsistent",
    "downgrade", "regress", "removed", "missing feature",
    "alternative", "switch from", "moving away", "leaving",
    "please fix", "still broken", "months now", "years now",
]

# Question patterns — people asking for help
QUESTION_PATTERNS = [
    r"\bhow (do|can|to)\b",
    r"\bwhy (does|is|do|did|won't|can't)\b",
    r"\bis there (a|any) way\b",
    r"\bhas anyone\b",
    r"\bdoes anyone\b",
    r"\bwhat('s| is) the best\b",
    r"\bany(one)? recommend\b",
]


def pain_score(text: str, title: str = "") -> int:
    """Score text 0-100 for how much it signals a painful problem."""
    if not text:
        return 0

    # Deprioritize newsletter/roundup posts — they hit tons of keywords but aren't problems
    title_lower = title.lower()
    if any(kw in title_lower for kw in ["news stories", "newsletter", "weekly recap",
                                         "this week's top", "roundup", "news round"]):
        return 2

    lower = text.lower()
    score = 0

    # Pain word matches (up to 50 pts)
    hits = sum(1 for w in PAIN_WORDS if w in lower)
    score += min(hits * 6, 50)

    # Question patterns (up to 15 pts)
    q_hits = sum(1 for p in QUESTION_PATTERNS if re.search(p, lower))
    score += min(q_hits * 5, 15)

    # Punctuation intensity — !, ?, CAPS (up to 15 pts)
    excl = lower.count("!")
    ques = lower.count("?")
    caps_ratio = sum(1 for c in text if c.isupper()) / max(len(text), 1)
    score += min(excl * 2 + ques * 2, 10)
    if caps_ratio > 0.3 and len(text) > 20:
        score += 5

    # Length bonus — longer posts often contain more detail about pain (up to 10 pts)
    score += min(len(text) // 200, 10)

    # Negative sentiment boosters (up to 10 pts)
    neg_phrases = ["i hate", "so sick of", "fed up", "give up", "last straw",
                   "deal breaker", "unacceptable", "ridiculous"]
    neg_hits = sum(1 for p in neg_phrases if p in lower)
    score += min(neg_hits * 5, 10)

    return min(score, 100)


# ---------------------------------------------------------------------------
# Reddit fetching
# ---------------------------------------------------------------------------

def fetch_posts(subreddit: str, sort: str = "hot", limit: int = 50,
                time_filter: str = "week", max_pages: int = 1) -> List[Dict]:
    """Fetch posts from a subreddit with pagination."""
    posts = []
    after = None
    per_page = min(limit, 100)

    for page in range(max_pages):
        params = {"limit": str(per_page)}
        if sort in ("top", "controversial"):
            params["t"] = time_filter
        if after:
            params["after"] = after

        try:
            data = reddit_json(f"/r/{subreddit}/{sort}", params)
        except Exception as e:
            sys.stderr.write(f"  [error] Page {page+1} failed: {e}\n")
            break

        children = data.get("data", {}).get("children", [])
        if not children:
            break

        for child in children:
            d = child.get("data", {})
            created = datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc)
            title = d.get("title", "")
            selftext = d.get("selftext", "")[:2000]
            text = f"{title} {selftext}"
            posts.append({
                "id": d.get("id"),
                "title": title,
                "selftext": selftext,
                "author": d.get("author", "[deleted]"),
                "score": d.get("score", 0),
                "num_comments": d.get("num_comments", 0),
                "url": f"https://reddit.com{d.get('permalink', '')}",
                "created": created.strftime("%Y-%m-%d"),
                "flair": d.get("link_flair_text", ""),
                "pain": pain_score(text, title=title),
            })

        after = data.get("data", {}).get("after")
        if not after:
            break  # no more pages

        sys.stderr.write(f"  Page {page+1}: {len(children)} posts (total so far: {len(posts)})\n")
        time.sleep(1.5)  # be polite between pages

    return posts


def fetch_comments(permalink: str, limit: int = 10) -> List[Dict]:
    """Fetch top comments for a post."""
    try:
        data = reddit_json(permalink, {"limit": str(limit), "sort": "top"})
    except Exception as e:
        sys.stderr.write(f"  [warn] comments fetch failed: {e}\n")
        return []

    if not isinstance(data, list) or len(data) < 2:
        return []

    comments = []
    for child in data[1].get("data", {}).get("children", []):
        d = child.get("data", {})
        body = d.get("body", "")
        if not body or child.get("kind") != "t1":
            continue
        comments.append({
            "author": d.get("author", "[deleted]"),
            "body": body[:1000],
            "score": d.get("score", 0),
            "pain": pain_score(body),
        })
    return comments


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def format_report(posts: List[Dict], subreddit: str, with_comments: bool) -> str:
    """Format the results as plain text."""
    lines = []
    lines.append("=" * 70)
    lines.append(f"  PROBLEM DISCOVERY — r/{subreddit}")
    lines.append(f"  Scanned {len(posts)} posts | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 70)

    if not posts:
        lines.append("\nNo posts found.\n")
        return "\n".join(lines)

    # Sort by composite: pain score (primary), engagement (secondary)
    for p in posts:
        engagement = p["score"] + p["num_comments"] * 2
        p["rank_score"] = p["pain"] * 3 + min(engagement, 200)

    posts.sort(key=lambda p: p["rank_score"], reverse=True)

    # Summary stats
    avg_pain = sum(p["pain"] for p in posts) / len(posts)
    high_pain = [p for p in posts if p["pain"] >= 40]
    lines.append(f"\n  Avg pain score: {avg_pain:.0f}/100")
    lines.append(f"  High-pain posts (>=40): {len(high_pain)}/{len(posts)}")
    lines.append("")

    # Top problems
    top = posts[:25]
    lines.append("-" * 70)
    lines.append("  TOP PAINFUL PROBLEMS")
    lines.append("-" * 70)

    for i, p in enumerate(top, 1):
        flair_tag = f" [{p['flair']}]" if p["flair"] else ""
        lines.append(f"\n  #{i}  [Pain: {p['pain']}]  {p['title']}{flair_tag}")
        lines.append(f"      {p['score']}↑  {p['num_comments']} comments  |  {p['created']}  by u/{p['author']}")
        lines.append(f"      {p['url']}")

        if p["selftext"]:
            # Show first ~300 chars of body
            body_preview = p["selftext"][:300].replace("\n", " ").strip()
            if len(p["selftext"]) > 300:
                body_preview += "..."
            lines.append(f"      > {body_preview}")

        if with_comments and p.get("comments"):
            pain_comments = sorted(p["comments"], key=lambda c: c["pain"], reverse=True)
            for c in pain_comments[:3]:
                if c["pain"] >= 20:
                    body_snip = c["body"][:200].replace("\n", " ").strip()
                    lines.append(f"        💬 [pain:{c['pain']}] u/{c['author']}: {body_snip}")

    # Theme clustering (simple keyword grouping)
    lines.append("\n" + "-" * 70)
    lines.append("  PAIN THEMES (keyword clusters)")
    lines.append("-" * 70)

    themes = {
        "Payments / Billing": ["payment", "billing", "charge", "refund", "subscription", "invoice", "tax"],
        "Shipping / Fulfillment": ["shipping", "fulfillment", "delivery", "tracking", "carrier", "order"],
        "Apps / Integrations": ["app", "plugin", "integration", "api", "webhook", "third party"],
        "Theme / Design": ["theme", "design", "template", "css", "layout", "customiz"],
        "SEO / Marketing": ["seo", "google", "traffic", "marketing", "ads", "conversion"],
        "Performance / Speed": ["slow", "speed", "performance", "loading", "lag"],
        "Support / Service": ["support", "response", "help", "customer service", "ticket"],
        "Sales / Revenue": ["sales", "revenue", "conversion", "drop", "decline"],
    }

    theme_counts = []
    for theme, keywords in themes.items():
        matching_posts = []
        for p in high_pain:
            ptxt = f"{p['title']} {p['selftext']}".lower()
            if any(k in ptxt for k in keywords):
                matching_posts.append(p)
        if matching_posts:
            examples = [p["title"][:70] for p in matching_posts[:3]]
            theme_counts.append((theme, len(matching_posts), examples))

    theme_counts.sort(key=lambda t: t[1], reverse=True)
    for theme, count, examples in theme_counts:
        lines.append(f"\n  {theme} ({count} mentions)")
        for ex in examples:
            lines.append(f"    - {ex}")

    lines.append("\n" + "=" * 70)
    lines.append("  END OF REPORT")
    lines.append("=" * 70 + "\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Discover painful problems in a subreddit")
    parser.add_argument("--subreddit", "-s", default="shopify", help="Subreddit to scan (default: shopify)")
    parser.add_argument("--sort", default="hot,new", help="Sort modes, comma-separated: hot,new,top (default: hot,new)")
    parser.add_argument("--limit", "-n", type=int, default=50, help="Posts per sort mode (default: 50)")
    parser.add_argument("--time", "-t", default="week", help="Time filter for top/controversial (default: week)")
    parser.add_argument("--pages", "-p", type=int, default=1, help="Pages per sort mode, 100 posts each (default: 1, max ~10)")
    parser.add_argument("--deep", action="store_true", help="Also fetch comments for top-scoring posts")
    parser.add_argument("--out", "-o", help="Output file (default: stdout)")
    args = parser.parse_args()

    sorts = [s.strip() for s in args.sort.split(",")]

    # Fetch posts from each sort mode
    all_posts = {}
    for sort in sorts:
        target = args.limit * args.pages
        sys.stderr.write(f"Fetching r/{args.subreddit}/{sort} (up to {target} posts, {args.pages} page(s))...\n")
        try:
            posts = fetch_posts(args.subreddit, sort=sort, limit=args.limit,
                                time_filter=args.time, max_pages=args.pages)
            for p in posts:
                all_posts[p["id"]] = p  # dedupe by id
            sys.stderr.write(f"  Got {len(posts)} posts\n")
        except Exception as e:
            sys.stderr.write(f"  [error] Failed to fetch {sort}: {e}\n")
        time.sleep(1)  # be polite to Reddit

    posts = list(all_posts.values())

    # Optionally fetch comments for high-pain posts
    if args.deep:
        high = sorted(posts, key=lambda p: p["pain"], reverse=True)[:15]
        sys.stderr.write(f"Fetching comments for {len(high)} high-pain posts...\n")
        for p in high:
            permalink = p["url"].replace("https://reddit.com", "")
            comments = fetch_comments(permalink)
            p["comments"] = comments
            # Boost pain score based on painful comments
            comment_pain = sum(c["pain"] for c in comments) / max(len(comments), 1)
            p["pain"] = min(100, p["pain"] + int(comment_pain * 0.3))
            time.sleep(1)

    # Generate report
    report = format_report(posts, args.subreddit, with_comments=args.deep)

    if args.out:
        with open(args.out, "w") as f:
            f.write(report)
        sys.stderr.write(f"Report written to {args.out}\n")
    else:
        print(report)


if __name__ == "__main__":
    main()
