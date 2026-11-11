import os
import re
import json
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

import requests
import feedparser

# ------------------------------------------------------------------
# SECRETS (your repo uses TG_TOKEN and TG_CHAT, other names work too)
# ------------------------------------------------------------------
BOT_TOKEN = (
    os.environ.get("TG_TOKEN")
    or os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("BOT_TOKEN")
)
CHAT_ID = (
    os.environ.get("TG_CHAT")
    or os.environ.get("TELEGRAM_CHAT_ID")
    or os.environ.get("CHAT_ID")
)
if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit(
        "Missing Telegram token or chat id. Check that the repository secrets "
        "TG_TOKEN and TG_CHAT exist and that the workflow env block passes them in."
    )

SEND_TEST = os.environ.get("SEND_TEST", "").lower() == "true"

# ------------------------------------------------------------------
# SETTINGS
# ------------------------------------------------------------------
MAX_AGE_HOURS = 72          # ignore items older than this
MAX_INSTANT_PER_RUN = 15    # cap on instant alerts per run, overflow goes to the digest
DIGEST_HOUR_UTC = 6         # daily digest goes out after this UTC hour
STATE_DIR = "state"

# Tier 1: always sent instantly
TIER1_PATTERNS = [
    r"alpha tau(?! omega)",
    r"alpha[\s\-]?dart",
    r"alphadart",
    r"\$drts\b",
    r"אלפא טאו",
]

# Phrases removed before matching so they can never trigger anything
NOISE_PHRASES = [
    "alpha tau omega",
]

# Tier 2: bare "drts" is sent instantly only if one of these words is also present
DRTS_BARE = re.compile(r"\bdrts\b")
CONTEXT_WORDS = [
    "stock", "shares", "share price", "biotech", "nasdaq", "tase", "ticker",
    "pps", "price target", "warrant", "offering", "earnings", "short",
    "tolmar", "keytruda", "pembro", "pembrolizumab", "gbm", "glioblastoma",
    "cscc", "head and neck", "pancreatic", "ahns", "sno", "asco", "fda",
    "ide", "pma", "breakthrough", "restart", "regain", "acapella",
    "impact trial", "admire", "brachytherapy", "radiation", "tau", "dart",
    "barclays", "insider", "form 4", "10b5-1", "index", "ta-90", "ta-125",
]

# Anything matching "drts" without a context word goes to the daily digest (tier 3)

# Sources. No Stocktwits on purpose.
REDDIT_QUERIES = [
    "Alpha Tau",
    "Alpha DaRT",
    "Alpha Tau Medical",
    "DRTS stock",
    "$DRTS",
]
# (query, hl, gl, ceid)
GOOGLE_NEWS_QUERIES = [
    ("Alpha Tau Medical", "en-US", "US", "US:en"),
    ("Alpha DaRT", "en-US", "US", "US:en"),
    ("DRTS Alpha Tau", "en-US", "US", "US:en"),
    ("אלפא טאו", "he", "IL", "IL:he"),
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; drts-monitor/1.0)"}
TIMEOUT = 25


# ------------------------------------------------------------------
# STATE HELPERS
# ------------------------------------------------------------------
def load_json(name, default):
    path = os.path.join(STATE_DIR, name)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(name, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def now_utc():
    return datetime.now(timezone.utc)


def struct_to_dt(t):
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc)


# ------------------------------------------------------------------
# TELEGRAM
# ------------------------------------------------------------------
def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for chunk in chunks:
        try:
            r = requests.post(
                url,
                data={
                    "chat_id": CHAT_ID,
                    "text": chunk,
                    "disable_web_page_preview": "true",
                },
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                print("Telegram error:", r.status_code, r.text[:200])
        except Exception as e:
            print("Telegram exception:", e)
        time.sleep(1)


# ------------------------------------------------------------------
# SOURCES
# Each fetcher returns a list of dicts:
# {id, source, title, text, url, published}
# ------------------------------------------------------------------
def fetch_reddit(query):
    url = f"https://www.reddit.com/search.rss?q={quote_plus(query)}&sort=new&t=week"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Reddit HTTP {r.status_code}")
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries:
        items.append({
            "id": "reddit:" + (e.get("id") or e.get("link", "")),
            "source": "Reddit",
            "title": e.get("title", ""),
            "text": (e.get("title", "") + " " + e.get("summary", "")),
            "url": e.get("link", ""),
            "published": struct_to_dt(e.get("updated_parsed") or e.get("published_parsed")),
        })
    return items


def fetch_google_news(query, hl, gl, ceid):
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + f"&hl={hl}&gl={gl}&ceid={ceid}"
    )
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Google News HTTP {r.status_code}")
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries:
        items.append({
            "id": "gnews:" + (e.get("id") or e.get("link", "")),
            "source": "News",
            "title": e.get("title", ""),
            "text": (e.get("title", "") + " " + e.get("summary", "")),
            "url": e.get("link", ""),
            "published": struct_to_dt(e.get("published_parsed")),
        })
    return items


# ------------------------------------------------------------------
# CLASSIFICATION
# ------------------------------------------------------------------
def classify(text):
    t = text.lower()
    for n in NOISE_PHRASES:
        t = t.replace(n, " ")
    for p in TIER1_PATTERNS:
        if re.search(p, t):
            return 1
    if DRTS_BARE.search(t):
        if any(c in t for c in CONTEXT_WORDS):
            return 2
        return 3
    return None


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
def fresh_stats():
    return {"checked": 0, "instant": 0, "digest": 0, "errors": {}}


def main():
    if SEND_TEST:
        send_telegram("DRTS monitor test: the bot is connected and working.")

    seen = load_json("seen.json", {})
    digest = load_json("digest.json", {
        "items": [],
        "last_digest_date": "",
        "stats": fresh_stats(),
    })
    stats = digest.setdefault("stats", fresh_stats())
    for k, v in fresh_stats().items():
        stats.setdefault(k, v)

    now = now_utc()
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)

    # prune seen ids older than 14 days
    keep_after = now - timedelta(days=14)
    seen = {
        k: v for k, v in seen.items()
        if datetime.fromisoformat(v) > keep_after
    }

    # collect from all sources, one failure never stops the others
    jobs = []
    for q in REDDIT_QUERIES:
        jobs.append((f"Reddit [{q}]", lambda q=q: fetch_reddit(q)))
    for q, hl, gl, ceid in GOOGLE_NEWS_QUERIES:
        jobs.append((
            f"News [{q}]",
            lambda q=q, hl=hl, gl=gl, ceid=ceid: fetch_google_news(q, hl, gl, ceid),
        ))

    collected = {}
    for name, fn in jobs:
        try:
            for item in fn():
                collected[item["id"]] = item
        except Exception as e:
            print(f"{name} failed: {e}")
            stats["errors"][name] = stats["errors"].get(name, 0) + 1
        time.sleep(2)

    instant, to_digest = [], []
    for item in collected.values():
        if item["id"] in seen:
            continue
        pub = item["published"]
        if pub and pub < cutoff:
            continue
        stats["checked"] += 1
        tier = classify(item["text"])
        seen[item["id"]] = now.isoformat()
        if tier in (1, 2):
            instant.append(item)
        elif tier == 3:
            to_digest.append(item)

    # newest first, cap instant alerts, overflow goes to the digest
    instant.sort(key=lambda i: i["published"] or now, reverse=True)
    overflow = instant[MAX_INSTANT_PER_RUN:]
    instant = instant[:MAX_INSTANT_PER_RUN]
    to_digest.extend(overflow)

    for item in instant:
        msg = f"[DRTS] {item['source']}\n{item['title']}\n{item['url']}"
        send_telegram(msg)
        stats["instant"] += 1

    for item in to_digest:
        digest["items"].append({
            "source": item["source"],
            "title": item["title"][:200],
            "url": item["url"],
        })
        stats["digest"] += 1

    # daily digest plus heartbeat
    today = now.strftime("%Y-%m-%d")
    if now.hour >= DIGEST_HOUR_UTC and digest.get("last_digest_date") != today:
        lines = ["DRTS monitor daily digest"]
        lines.append(
            f"Checked {stats['checked']} new items. "
            f"Sent {stats['instant']} instantly, {stats['digest']} loose matches below."
        )
        if stats["errors"]:
            errs = ", ".join(f"{k} x{v}" for k, v in stats["errors"].items())
            lines.append(f"Source errors: {errs}")
        else:
            lines.append("No source errors.")
        if digest["items"]:
            lines.append("")
            lines.append("Loose matches (bare 'drts' with no stock context):")
            for it in digest["items"][:40]:
                lines.append(f"- {it['source']}: {it['title']}\n  {it['url']}")
        send_telegram("\n".join(lines))
        digest["items"] = []
        digest["last_digest_date"] = today
        digest["stats"] = fresh_stats()

    save_json("seen.json", seen)
    save_json("digest.json", digest)
    print(f"Done. instant={len(instant)} digest_added={len(to_digest)}")


if __name__ == "__main__":
    main()
