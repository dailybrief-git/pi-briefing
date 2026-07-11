#!/usr/bin/env python3
"""
Personal Intelligence Dashboard - daily briefing generator.

Runs unattended on GitHub Actions. Steps:
  1. Load profile.json, seen-stories.json and the previous day's HTML (used as
     the visual template).
  2. Fetch fresh results from the Brave Search API for queries derived from the
     profile (Thai/Chiang Mai sweep is mandatory every run).
  3. Ask the Anthropic API to write today's briefing as a complete HTML page
     (same design as the template) plus a machine-readable state delta.
  4. Validate the output hard. If anything looks wrong, exit non-zero WITHOUT
     writing anything, so the workflow never publishes a broken page.
  5. Write dashboard_YYYY-MM-DD.html + dashboard_latest.html and update
     seen-stories.json.

Secrets come from environment variables (set as GitHub Actions secrets):
  ANTHROPIC_API_KEY   - required
  BRAVE_API_KEY       - required
Optional env:
  MODEL               - Anthropic model id (default below)
  OUTPUT_DIR          - where files are written (default: current dir)
  SEARCH_MAX          - max Brave queries per run (default 22)
  MAX_TOKENS          - Anthropic max output tokens (default 32000)
"""

import json
import os
import re
import sys
import time
import datetime
import urllib.error
import urllib.parse
import urllib.request

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

import anthropic

# ---------------------------------------------------------------- config -----

MODEL = os.environ.get("MODEL", "claude-sonnet-5")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", ".")
SEARCH_MAX = int(os.environ.get("SEARCH_MAX", "22"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "32000"))
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
TIMEZONE = "Asia/Bangkok"

HTML_BEGIN, HTML_END = "===HTML_BEGIN===", "===HTML_END==="
STATE_BEGIN, STATE_END = "===STATE_BEGIN===", "===STATE_END==="

# Sentinels that must survive into the generated page. If any is missing the
# page is broken (this is exactly how earlier truncation bugs were caught).
REQUIRED_MARKERS = ["Tune feed", "Copy feedback for Claude", "budget-track"]
MIN_HTML_LEN = 20000


def log(msg):
    print(msg, flush=True)


def die(msg):
    log("FATAL: " + msg)
    sys.exit(1)


# ------------------------------------------------------------- load state ----

def salvage_seen(text):
    """Recover a truncated seen-stories.json by collecting only the complete
    story objects and dropping any incomplete trailing one. Anti-repetition
    memory should degrade gracefully, never crash the run."""
    note = ""
    m = re.search(r'"_note"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        note = m.group(1)
    lb = text.find("[", text.find('"stories"'))
    stories, i, n = [], lb + 1, len(text)
    depth, start, instr, esc = 0, None, False, False
    while i < n:
        c = text[i]
        if instr:
            esc = (c == "\\") and not esc
            if c == '"' and not esc:
                instr = False
        elif c == '"':
            instr = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    stories.append(json.loads(text[start:i + 1]))
                except Exception:
                    pass
                start = None
        elif c == "]" and depth == 0:
            break
        i += 1
    return {"_note": note, "stories": stories}


def load_inputs():
    def read(path):
        p = os.path.join(OUTPUT_DIR, path)
        if not os.path.exists(p):
            die("missing required file: " + path)
        with open(p, "r", encoding="utf-8") as f:
            return f.read()

    profile = json.loads(read("profile.json"))
    seen_raw = read("seen-stories.json")
    try:
        seen = json.loads(seen_raw)
    except Exception as e:
        log("seen-stories.json invalid (%s) - salvaging complete entries" % e)
        seen = salvage_seen(seen_raw)
        log("salvaged %d stories" % len(seen.get("stories", [])))
    template = read("dashboard_latest.html")
    return profile, seen, template


def now_bangkok():
    if ZoneInfo:
        return datetime.datetime.now(ZoneInfo(TIMEZONE))
    # Fallback: UTC+7 fixed offset.
    return datetime.datetime.utcnow() + datetime.timedelta(hours=7)


# ------------------------------------------------------------ build queries --

def build_queries(profile):
    """Prioritised, de-duplicated query list. Thai/Chiang Mai sweep first
    (mandatory per the learned-preferences rule), then intelligence, then the
    lighter-interest topics."""
    q = []

    # 1. Mandatory Thai / Chiang Mai sweep (operational + demand-side).
    q += [
        "Thailand tourism news this week",
        "Chiang Mai airport OR infrastructure OR road project 2026",
        "Thailand visa policy change tourism 2026",
        "Tourism Authority of Thailand TAT campaign 2026",
        "Chiang Mai weather flood warning TMD",
        "Thailand hospitality hotel business news",
        "Chinese outbound tourism Thailand trend 2026",
    ]

    # 2. Intelligence topics.
    for t in profile.get("intelligence_topics", []):
        term = t.split(" (")[0].split(" — ")[0].strip()
        q.append(term + " breakthrough 2026")

    # 3. Company watchlist (innovation only).
    for c in profile.get("company_watchlist", {}).get("companies", []):
        q.append(c + " new product OR breakthrough announcement")

    # 4. Startup radar spaces.
    for s in profile.get("startup_radar", {}).get("spaces", [])[:3]:
        term = s.split(" (")[0].strip()
        q.append(term + " startup launch funding 2026")

    # 5. Personal interests (lighter weight, a rotating handful).
    weekday = now_bangkok().weekday()
    interests = profile.get("personal_interests", [])
    if interests:
        pick = [interests[(weekday + i) % len(interests)] for i in range(4)]
        q += [p + " latest news 2026" for p in pick]

    # 6. Podcasts (new-episode discovery).
    for show in profile.get("podcasts", {}).get("shows", []):
        name = show.split(" (")[0].strip()
        q.append(name + " latest episode")

    # De-dupe preserving order, then cap.
    seen_q, out = set(), []
    for item in q:
        k = item.lower()
        if k not in seen_q:
            seen_q.add(k)
            out.append(item)
    return out[:SEARCH_MAX]


# ------------------------------------------------------------- brave search --

def brave_search(query, token, count=5):
    """Query Brave. Try with a freshness bias first; if the request is rejected
    for any reason, retry with only the essential params so one bad optional
    parameter can never wipe out the whole run."""
    headers = {"Accept": "application/json", "X-Subscription-Token": token}
    attempts = [
        {"q": query, "count": count, "freshness": "pw"},
        {"q": query, "count": count},
    ]
    for params in attempts:
        url = BRAVE_ENDPOINT + "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=30
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            log("  search HTTP %s for %r: %s" % (e.code, query, body))
            continue
        except Exception as e:
            log("  search failed for %r: %s" % (query, e))
            continue
        results = (data.get("web") or {}).get("results") or []
        out = []
        for r in results[:count]:
            out.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "desc": re.sub("<[^>]+>", "", r.get("description", "") or ""),
                    "age": r.get("age") or r.get("page_age") or "",
                }
            )
        return out
    return []


def gather_results(queries, token):
    digest = []
    for i, query in enumerate(queries, 1):
        log("[%2d/%d] %s" % (i, len(queries), query))
        hits = brave_search(query, token)
        if hits:
            digest.append({"query": query, "results": hits})
        time.sleep(1.1)  # stay under Brave's ~1 req/sec free-tier limit
    return digest


def results_to_text(digest):
    lines = []
    for block in digest:
        lines.append("### Search: " + block["query"])
        for r in block["results"]:
            age = (" (" + r["age"] + ")") if r["age"] else ""
            lines.append("- %s%s\n  %s\n  %s" % (r["title"], age, r["url"], r["desc"]))
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------- the prompt --

SYSTEM_PROMPT = """You are the generator for Anthony's Personal Intelligence \
Dashboard - a personalised daily briefing, NOT a news aggregator. It answers \
"what changed that matters to me?" not "what's the latest news?".

Follow these rules exactly:

DESIGN / OUTPUT
- Produce a COMPLETE, self-contained HTML page in the identical visual style, \
structure, CSS and JavaScript as the TEMPLATE you are given. Reuse the \
template's <style> and <script> blocks verbatim except for the date-stamped \
values below. Do not redesign anything.
- Update every date-stamped value to TODAY: the <title>, the eyebrow line, the \
<h1>/masthead date and lead paragraph, and the localStorage key \
(const KEY='pi-feedback-YYYY-MM-DD'). The feedback <script> block, the \
"Tune feed" controls and the "Copy feedback for Claude" button MUST remain \
present and functional - never drop them.
- In the footer, keep a small credit line "Search powered by the Brave Search \
API" (this attribution is required by the search provider).
- Keep the attention budget bar honest and in sync with the actual number of \
cards you output. Empty sections are allowed - say so, never pad.

EDITORIAL
- Respect the attention budget in the profile. End the briefing complete, not \
an infinite feed.
- Cluster duplicate coverage into ONE card per event. Label confidence \
(established fact vs. speculation vs. forecast). Frame Opportunities as \
hypotheses, not predictions.
- Anti-repetition: you are given the stories already briefed. Do NOT re-brief \
one unless there is a genuine development beyond "still true"; if there is, \
write it as a development that references the prior story.
- Honour the profile's learned_preferences, deprioritise list, source \
philosophy ("news before it's news", but big relevant mainstream stories still \
count) and the mandatory Thai/Chiang Mai sweep.
- Only include a fact if a provided search result supports it. Link real source \
URLs from the search results. Never invent a URL, a statistic, or a market \
figure. If you cannot verify something, leave it out.

RESPONSE FORMAT - output EXACTLY this and nothing else:
===HTML_BEGIN===
<!DOCTYPE html> ... entire page ... </html>
===HTML_END===
===STATE_BEGIN===
{"add": [{"id": "kebab-slug", "first_briefed": "YYYY-MM-DD", "section": \
"alerts|opportunities|major|gems|interest|companies|startups|markets|podcasts", \
"summary": "one line on what was briefed", "status": "active|closed"}], \
"run_summary": "2-3 sentence note on what led today and what was empty"}
===STATE_END==="""


def build_user_prompt(profile, seen, template, digest_text, dt):
    date_iso = dt.strftime("%Y-%m-%d")
    date_human = dt.strftime("%A, %-d %B %Y") if os.name != "nt" else dt.strftime("%A, %d %B %Y")
    return (
        "TODAY is %s (%s), timezone Asia/Bangkok, Chiang Mai.\n\n"
        "=== PROFILE (what is relevant; obey learned_preferences) ===\n%s\n\n"
        "=== ALREADY BRIEFED (anti-repetition memory) ===\n%s\n\n"
        "=== FRESH SEARCH RESULTS (your only sourced facts) ===\n%s\n\n"
        "=== TEMPLATE (reuse this exact design; restyle nothing) ===\n%s\n\n"
        "Now produce today's briefing in the required response format."
        % (
            date_human,
            date_iso,
            json.dumps(profile, ensure_ascii=False, indent=1),
            json.dumps(seen, ensure_ascii=False)[:12000],
            digest_text,
            template,
        )
    )


# ------------------------------------------------------------ call anthropic --

def generate(profile, seen, template, digest_text, dt):
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    log("calling Anthropic model %s ..." % MODEL)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt(
            profile, seen, template, digest_text, dt)}],
    )
    if resp.stop_reason == "max_tokens":
        die("model hit max_tokens - output truncated, refusing to publish. "
            "Raise MAX_TOKENS.")
    parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    return "".join(parts)


# ---------------------------------------------------------------- parse/check -

def extract(text, begin, end, what):
    i = text.find(begin)
    j = text.find(end)
    if i == -1 or j == -1 or j < i:
        die("could not find %s delimiters in model output" % what)
    return text[i + len(begin):j].strip()


def validate_html(html, dt):
    date_iso = dt.strftime("%Y-%m-%d")
    if not html.lstrip().startswith("<!DOCTYPE html"):
        die("HTML does not start with <!DOCTYPE html>")
    if not html.rstrip().endswith("</html>"):
        die("HTML does not end with </html> (likely truncated)")
    if len(html) < MIN_HTML_LEN:
        die("HTML suspiciously short (%d chars)" % len(html))
    for m in REQUIRED_MARKERS:
        if m not in html:
            die("HTML missing required marker: %r" % m)
    if ("pi-feedback-" + date_iso) not in html:
        die("HTML feedback key not stamped with today's date (%s)" % date_iso)
    log("HTML validated: %d chars" % len(html))


def merge_state(seen, state_json, dt):
    try:
        state = json.loads(state_json)
    except Exception as e:
        die("STATE block is not valid JSON: %s" % e)
    add = state.get("add", [])
    existing = {s.get("id") for s in seen.get("stories", [])}
    for item in add:
        if item.get("id") and item["id"] not in existing:
            seen.setdefault("stories", []).append(item)
    # Prune entries whose first_briefed is older than 30 days.
    cutoff = (dt.date() - datetime.timedelta(days=30)).isoformat()
    seen["stories"] = [
        s for s in seen.get("stories", [])
        if str(s.get("first_briefed", "")) >= cutoff or s.get("status") == "active"
    ]
    return seen, state.get("run_summary", "")


# ----------------------------------------------------------------------- main -

def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        die("ANTHROPIC_API_KEY not set")
    brave = os.environ.get("BRAVE_API_KEY")
    if not brave:
        die("BRAVE_API_KEY not set")

    dt = now_bangkok()
    date_iso = dt.strftime("%Y-%m-%d")
    log("=== PI briefing run for %s (Asia/Bangkok) ===" % date_iso)

    profile, seen, template = load_inputs()

    queries = build_queries(profile)
    log("running %d searches" % len(queries))
    digest = gather_results(queries, brave)
    if not digest:
        die("no search results at all - aborting rather than publishing an "
            "empty/hallucinated briefing")
    digest_text = results_to_text(digest)

    raw = generate(profile, seen, template, digest_text, dt)

    html = extract(raw, HTML_BEGIN, HTML_END, "HTML")
    state_json = extract(raw, STATE_BEGIN, STATE_END, "STATE")
    validate_html(html, dt)
    seen, run_summary = merge_state(seen, state_json, dt)

    dated = os.path.join(OUTPUT_DIR, "dashboard_%s.html" % date_iso)
    latest = os.path.join(OUTPUT_DIR, "dashboard_latest.html")
    with open(dated, "w", encoding="utf-8") as f:
        f.write(html)
    with open(latest, "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(OUTPUT_DIR, "seen-stories.json"), "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)

    log("wrote %s and dashboard_latest.html" % os.path.basename(dated))
    log("run summary: " + (run_summary or "(none)"))
    log("=== done ===")


if __name__ == "__main__":
    main()
