#!/usr/bin/env python3
"""
Pain Signals — web UI for Reddit problem discovery.
Run: python app.py
Then open: http://localhost:5001/pain-signals
"""

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from flask import Flask, render_template_string, jsonify, request

app = Flask(__name__)

# ---------------------------------------------------------------------------
# HTTP (stdlib only, no requests needed)
# ---------------------------------------------------------------------------

USER_AGENT = "pain-signals/0.1 (research tool)"
MAX_RETRIES = 3
RETRY_DELAY = 2.0


def http_get(url, timeout=30, retries=MAX_RETRIES):
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
            time.sleep(delay)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
    raise last_err


def reddit_json(path, params=None):
    path = path.rstrip("/")
    if not path.endswith(".json"):
        path += ".json"
    qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"https://www.reddit.com{path}?raw_json=1" + (f"&{qs}" if qs else "")
    return http_get(url)


# ---------------------------------------------------------------------------
# Pain scoring
# ---------------------------------------------------------------------------

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

QUESTION_PATTERNS = [
    r"\bhow (do|can|to)\b", r"\bwhy (does|is|do|did|won't|can't)\b",
    r"\bis there (a|any) way\b", r"\bhas anyone\b", r"\bdoes anyone\b",
    r"\bwhat('s| is) the best\b", r"\bany(one)? recommend\b",
]

NEG_PHRASES = ["i hate", "so sick of", "fed up", "give up", "last straw",
               "deal breaker", "unacceptable", "ridiculous"]


def pain_score(text, title=""):
    if not text:
        return 0
    title_lower = title.lower()
    if any(kw in title_lower for kw in ["news stories", "newsletter", "weekly recap",
                                         "this week's top", "roundup", "news round"]):
        return 2
    lower = text.lower()
    score = 0
    score += min(sum(1 for w in PAIN_WORDS if w in lower) * 6, 50)
    score += min(sum(1 for p in QUESTION_PATTERNS if re.search(p, lower)) * 5, 15)
    score += min(lower.count("!") * 2 + lower.count("?") * 2, 10)
    caps_ratio = sum(1 for c in text if c.isupper()) / max(len(text), 1)
    if caps_ratio > 0.3 and len(text) > 20:
        score += 5
    score += min(len(text) // 200, 10)
    score += min(sum(1 for p in NEG_PHRASES if p in lower) * 5, 10)
    return min(score, 100)


def pain_label(score):
    if score >= 40:
        return "high"
    elif score >= 20:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Reddit fetching
# ---------------------------------------------------------------------------

def fetch_posts(subreddit, sort="hot", limit=100, time_filter="month", max_pages=3):
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
        except Exception:
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
            ps = pain_score(text, title=title)
            posts.append({
                "id": d.get("id"),
                "title": title,
                "selftext": selftext[:500],
                "author": d.get("author", "[deleted]"),
                "score": d.get("score", 0),
                "num_comments": d.get("num_comments", 0),
                "url": f"https://reddit.com{d.get('permalink', '')}",
                "created": created.strftime("%Y-%m-%d"),
                "flair": d.get("link_flair_text", ""),
                "pain": ps,
                "pain_label": pain_label(ps),
            })

        after = data.get("data", {}).get("after")
        if not after:
            break
        time.sleep(1.5)

    return posts


def fetch_comments(permalink, limit=10):
    try:
        data = reddit_json(permalink, {"limit": str(limit), "sort": "top"})
    except Exception:
        return []
    if not isinstance(data, list) or len(data) < 2:
        return []
    comments = []
    for child in data[1].get("data", {}).get("children", []):
        d = child.get("data", {})
        body = d.get("body", "")
        if not body or child.get("kind") != "t1":
            continue
        ps = pain_score(body)
        comments.append({
            "author": d.get("author", "[deleted]"),
            "body": body[:300],
            "score": d.get("score", 0),
            "pain": ps,
            "pain_label": pain_label(ps),
        })
    return comments


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

THEMES = {
    "Payments / Billing": ["payment", "billing", "charge", "refund", "subscription", "invoice", "tax"],
    "Shipping / Fulfillment": ["shipping", "fulfillment", "delivery", "tracking", "carrier", "order"],
    "Apps / Integrations": ["app", "plugin", "integration", "api", "webhook", "third party"],
    "Theme / Design": ["theme", "design", "template", "css", "layout", "customiz"],
    "SEO / Marketing": ["seo", "google", "traffic", "marketing", "ads", "conversion"],
    "Performance / Speed": ["slow", "speed", "performance", "loading", "lag"],
    "Support / Service": ["support", "response", "help", "customer service", "ticket"],
    "Sales / Revenue": ["sales", "revenue", "conversion", "drop", "decline"],
    "Fraud / Security": ["fraud", "scam", "chargeback", "stolen", "hack", "phishing", "dispute"],
}


def analyze(subreddit):
    """Run full analysis, return structured results."""
    all_posts = {}
    for sort in ["hot", "new", "top"]:
        try:
            posts = fetch_posts(subreddit, sort=sort, limit=100, time_filter="month", max_pages=3)
            for p in posts:
                all_posts[p["id"]] = p
        except Exception:
            pass
        time.sleep(1)

    posts = list(all_posts.values())

    # Fetch comments for top 10 pain posts
    top_pain = sorted(posts, key=lambda p: p["pain"], reverse=True)[:10]
    for p in top_pain:
        permalink = p["url"].replace("https://reddit.com", "")
        p["comments"] = fetch_comments(permalink)
        comment_pain = sum(c["pain"] for c in p["comments"]) / max(len(p["comments"]), 1)
        p["pain"] = min(100, p["pain"] + int(comment_pain * 0.3))
        p["pain_label"] = pain_label(p["pain"])
        time.sleep(1)

    # Sort by composite rank
    for p in posts:
        engagement = p["score"] + p["num_comments"] * 2
        p["rank_score"] = p["pain"] * 3 + min(engagement, 200)
    posts.sort(key=lambda p: p["rank_score"], reverse=True)

    # Theme analysis on high-pain posts
    high_pain = [p for p in posts if p["pain"] >= 20]
    theme_results = []
    for theme, keywords in THEMES.items():
        matching = []
        for p in high_pain:
            ptxt = f"{p['title']} {p['selftext']}".lower()
            if any(k in ptxt for k in keywords):
                matching.append({"title": p["title"][:80], "url": p["url"], "pain": p["pain"]})
        if matching:
            theme_results.append({"name": theme, "count": len(matching), "posts": matching[:5]})
    theme_results.sort(key=lambda t: t["count"], reverse=True)

    return {
        "subreddit": subreddit,
        "total_posts": len(posts),
        "high_pain_count": len(high_pain),
        "avg_pain": round(sum(p["pain"] for p in posts) / max(len(posts), 1)),
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "top_posts": posts[:25],
        "themes": theme_results,
    }


# ---------------------------------------------------------------------------
# In-memory job tracking (simple, no DB needed)
# ---------------------------------------------------------------------------

jobs = {}


def run_job(job_id, subreddit):
    try:
        jobs[job_id]["status"] = "running"
        result = analyze(subreddit)
        jobs[job_id]["status"] = "done"
        jobs[job_id]["result"] = result
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/pain-signals")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/pain-signals/api/scan", methods=["POST"])
def start_scan():
    data = request.get_json() or {}
    raw = data.get("subreddit", "").strip()
    # Accept full URLs like reddit.com/r/shopify or just "shopify"
    match = re.search(r"r/([a-zA-Z0-9_]+)", raw)
    subreddit = match.group(1) if match else raw.strip("/")
    if not subreddit or not re.match(r"^[a-zA-Z0-9_]+$", subreddit):
        return jsonify({"error": "Invalid subreddit name"}), 400

    job_id = f"{subreddit}_{int(time.time())}"
    jobs[job_id] = {"status": "queued", "subreddit": subreddit}
    thread = threading.Thread(target=run_job, args=(job_id, subreddit), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/pain-signals/api/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pain Signals</title>
<style>
  :root {
    --bg: #0a0a0a; --surface: #141414; --border: #252525;
    --text: #e0e0e0; --text-muted: #888; --accent: #ff6b35;
    --pain-high: #ff4444; --pain-med: #ffaa00; --pain-low: #666;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.6;
    min-height: 100vh;
  }
  .container { max-width: 900px; margin: 0 auto; padding: 2rem 1.5rem; }

  /* Header */
  h1 { font-size: 1.8rem; font-weight: 700; margin-bottom: 0.25rem; }
  h1 span { color: var(--accent); }
  .subtitle { color: var(--text-muted); font-size: 0.95rem; margin-bottom: 2rem; }

  /* Input */
  .input-row {
    display: flex; gap: 0.75rem; margin-bottom: 2rem;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 0.5rem;
  }
  .input-row input {
    flex: 1; background: none; border: none; color: var(--text);
    font-size: 1rem; padding: 0.75rem; outline: none;
  }
  .input-row input::placeholder { color: var(--text-muted); }
  .input-row button {
    background: var(--accent); color: #fff; border: none; border-radius: 8px;
    padding: 0.75rem 1.5rem; font-size: 0.95rem; font-weight: 600;
    cursor: pointer; white-space: nowrap; transition: opacity 0.2s;
  }
  .input-row button:hover { opacity: 0.85; }
  .input-row button:disabled { opacity: 0.4; cursor: not-allowed; }

  /* Status */
  .status {
    text-align: center; padding: 3rem 1rem; color: var(--text-muted);
  }
  .status .spinner {
    display: inline-block; width: 24px; height: 24px;
    border: 3px solid var(--border); border-top-color: var(--accent);
    border-radius: 50%; animation: spin 0.8s linear infinite;
    margin-bottom: 1rem;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Stats bar */
  .stats {
    display: flex; gap: 1.5rem; flex-wrap: wrap;
    margin-bottom: 2rem; padding: 1rem 1.25rem;
    background: var(--surface); border-radius: 12px; border: 1px solid var(--border);
  }
  .stat { display: flex; flex-direction: column; }
  .stat-val { font-size: 1.5rem; font-weight: 700; color: var(--accent); }
  .stat-label { font-size: 0.8rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }

  /* Sections */
  .section-title {
    font-size: 1.1rem; font-weight: 700; margin: 2rem 0 1rem;
    padding-bottom: 0.5rem; border-bottom: 1px solid var(--border);
  }

  /* Post cards */
  .post {
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 1.25rem; margin-bottom: 0.75rem; transition: border-color 0.2s;
  }
  .post:hover { border-color: #333; }
  .post-header { display: flex; align-items: flex-start; gap: 0.75rem; margin-bottom: 0.5rem; }
  .pain-badge {
    flex-shrink: 0; padding: 0.2rem 0.6rem; border-radius: 6px;
    font-size: 0.75rem; font-weight: 700; text-transform: uppercase;
  }
  .pain-badge.high { background: rgba(255,68,68,0.15); color: var(--pain-high); }
  .pain-badge.medium { background: rgba(255,170,0,0.15); color: var(--pain-med); }
  .pain-badge.low { background: rgba(100,100,100,0.15); color: var(--pain-low); }
  .post-title {
    font-weight: 600; font-size: 0.95rem; line-height: 1.4;
  }
  .post-title a { color: var(--text); text-decoration: none; }
  .post-title a:hover { color: var(--accent); }
  .post-meta {
    display: flex; gap: 1rem; font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.5rem;
  }
  .post-body {
    font-size: 0.85rem; color: var(--text-muted); line-height: 1.5;
    max-height: 4.5em; overflow: hidden; position: relative;
  }
  .post-body::after {
    content: ''; position: absolute; bottom: 0; left: 0; right: 0;
    height: 1.5em; background: linear-gradient(transparent, var(--surface));
  }
  .flair {
    display: inline-block; font-size: 0.7rem; padding: 0.1rem 0.5rem;
    background: rgba(255,255,255,0.06); border-radius: 4px; color: var(--text-muted);
  }

  /* Comments */
  .comment {
    margin: 0.5rem 0 0 1.5rem; padding: 0.5rem 0.75rem;
    border-left: 2px solid var(--border); font-size: 0.82rem;
  }
  .comment .author { color: var(--text-muted); }
  .comment .body { color: var(--text); margin-top: 0.25rem; }

  /* Themes */
  .themes { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 0.75rem; }
  .theme-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 1rem; transition: border-color 0.2s;
  }
  .theme-card:hover { border-color: #333; }
  .theme-name { font-weight: 700; font-size: 0.95rem; margin-bottom: 0.25rem; }
  .theme-count { font-size: 0.8rem; color: var(--accent); margin-bottom: 0.5rem; }
  .theme-post {
    font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.25rem;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .theme-post a { color: var(--text-muted); text-decoration: none; }
  .theme-post a:hover { color: var(--text); }
</style>
</head>
<body>
<div class="container">
  <h1><span>Pain</span> Signals</h1>
  <p class="subtitle">Scan any subreddit for painful problems people are talking about</p>

  <div class="input-row">
    <input type="text" id="sub-input" placeholder="Enter subreddit — e.g. shopify, ecommerce, or reddit.com/r/shopify" autofocus>
    <button id="scan-btn" onclick="startScan()">Scan</button>
  </div>

  <div id="status"></div>
  <div id="results"></div>
</div>

<script>
const $ = (s) => document.querySelector(s);

function startScan() {
  const val = $('#sub-input').value.trim();
  if (!val) return;
  $('#scan-btn').disabled = true;
  $('#scan-btn').textContent = 'Scanning...';
  $('#status').innerHTML = '<div class="spinner"></div><div>Scanning posts and comments... this takes about 60 seconds</div>';
  $('#results').innerHTML = '';

  fetch('/pain-signals/api/scan', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({subreddit: val})
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) throw new Error(data.error);
    pollJob(data.job_id);
  })
  .catch(err => {
    $('#status').innerHTML = `<div style="color:var(--pain-high)">Error: ${err.message}</div>`;
    resetBtn();
  });
}

$('#sub-input').addEventListener('keydown', e => { if (e.key === 'Enter') startScan(); });

function pollJob(jobId) {
  fetch(`/pain-signals/api/status/${jobId}`)
  .then(r => r.json())
  .then(data => {
    if (data.status === 'done') {
      $('#status').innerHTML = '';
      renderResults(data.result);
      resetBtn();
    } else if (data.status === 'error') {
      $('#status').innerHTML = `<div style="color:var(--pain-high)">Error: ${data.error}</div>`;
      resetBtn();
    } else {
      setTimeout(() => pollJob(jobId), 2000);
    }
  });
}

function resetBtn() {
  $('#scan-btn').disabled = false;
  $('#scan-btn').textContent = 'Scan';
}

function renderResults(r) {
  let html = '';

  // Stats
  html += `<div class="stats">
    <div class="stat"><div class="stat-val">${r.total_posts}</div><div class="stat-label">Posts scanned</div></div>
    <div class="stat"><div class="stat-val">${r.high_pain_count}</div><div class="stat-label">High-pain posts</div></div>
    <div class="stat"><div class="stat-val">${r.avg_pain}/100</div><div class="stat-label">Avg pain score</div></div>
    <div class="stat"><div class="stat-val">r/${r.subreddit}</div><div class="stat-label">Subreddit</div></div>
  </div>`;

  // Themes
  if (r.themes.length) {
    html += '<div class="section-title">Pain Themes</div><div class="themes">';
    for (const t of r.themes) {
      html += `<div class="theme-card">
        <div class="theme-name">${esc(t.name)}</div>
        <div class="theme-count">${t.count} post${t.count !== 1 ? 's' : ''}</div>`;
      for (const p of t.posts) {
        html += `<div class="theme-post"><a href="${esc(p.url)}" target="_blank">${esc(p.title)}</a></div>`;
      }
      html += '</div>';
    }
    html += '</div>';
  }

  // Posts
  html += '<div class="section-title">Top Pain Posts</div>';
  for (const p of r.top_posts) {
    html += `<div class="post">
      <div class="post-header">
        <span class="pain-badge ${p.pain_label}">${p.pain_label}</span>
        <div class="post-title"><a href="${esc(p.url)}" target="_blank">${esc(p.title)}</a></div>
      </div>
      <div class="post-meta">
        <span>${p.score} upvotes</span>
        <span>${p.num_comments} comments</span>
        <span>${p.created}</span>
        <span>u/${esc(p.author)}</span>
        ${p.flair ? `<span class="flair">${esc(p.flair)}</span>` : ''}
      </div>`;
    if (p.selftext) {
      html += `<div class="post-body">${esc(p.selftext)}</div>`;
    }
    if (p.comments && p.comments.length) {
      const painComments = p.comments.filter(c => c.pain >= 15).slice(0, 3);
      for (const c of painComments) {
        html += `<div class="comment">
          <div class="author">u/${esc(c.author)} · ${c.score} pts · <span class="pain-badge ${c.pain_label}" style="font-size:0.7rem">${c.pain_label}</span></div>
          <div class="body">${esc(c.body)}</div>
        </div>`;
      }
    }
    html += '</div>';
  }

  $('#results').innerHTML = html;
}

function esc(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("Starting Pain Signals at http://localhost:5001/pain-signals")
    app.run(host="0.0.0.0", port=5001, debug=True)
