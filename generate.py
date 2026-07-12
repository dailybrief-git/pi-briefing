#!/usr/bin/env python3
"""
Personal Intelligence Dashboard - daily briefing generator.

Runs unattended on GitHub Actions. Steps:
  1. Load profile.json, seen-stories.json and the previous day's HTML (used for
     its static chrome: <style>, sidebar, feedback <script>).
  2. Fetch fresh results from the Brave Search API for queries derived from the
     profile (Thai/Chiang Mai sweep is mandatory every run).
  3. Ask the Anthropic API to return the briefing as COMPACT JSON - just the
     content, no HTML. (This is what keeps runs fast and stops the model from
     ever running away trying to reproduce a whole HTML page.)
  4. Deterministically render that JSON into the full HTML page in Python.
  5. Validate hard; if anything looks wrong, exit non-zero WITHOUT writing, so
     the workflow never publishes a broken page.
  6. Write dashboard_YYYY-MM-DD.html + dashboard_latest.html, update seen-stories.

Secrets come from environment variables (GitHub Actions secrets):
  ANTHROPIC_API_KEY, BRAVE_API_KEY   - required
Optional env: MODEL, OUTPUT_DIR, SEARCH_MAX, MAX_TOKENS
"""

import csv
import datetime
import html as htmllib
import io
import json
import os
import re
import sys
import time
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
USERS_DIR = os.path.join(OUTPUT_DIR, "users")
TEMPLATE_PATH = os.environ.get("TEMPLATE", os.path.join(OUTPUT_DIR, "template.html"))
SEARCH_MAX = int(os.environ.get("SEARCH_MAX", "22"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "26000"))
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
TIMEZONE = "Asia/Bangkok"

JSON_BEGIN, JSON_END = "===JSON_BEGIN===", "===JSON_END==="

REQUIRED_MARKERS = ["Tune feed", "Submit feedback to Claude", "budget-track", "PROFILE_LISTS", "PROFILE_USER", "nav-profile"]
MIN_HTML_LEN = 20000

# id, H2 heading, section note, accent css-var, wwuw summary label, relevance label
SECTIONS = [
    ("alerts", "Alerts", "Bypasses the normal briefing", "alert",
     "Why it matters / what to watch", "Relevance"),
    ("opportunities", "Your Opportunities", "Hypotheses, not predictions",
     "opportunity", "Hypothesis &amp; what to check", "Relevance"),
    ("major", "Major Developments", "What / why / uncertain / watch", "major",
     "What / why / uncertain / watch", "Impact on you"),
]
REL_CLASS = {"High": "rel-high", "Med": "rel-med", "Low": "rel-low"}


class PipelineError(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def die(msg):
    raise PipelineError(msg)


# ------------------------------------------------------------- load state ----

def salvage_seen(text):
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


def read_template():
    path = TEMPLATE_PATH
    if not os.path.exists(path):
        alt = os.path.join(OUTPUT_DIR, "dashboard_latest.html")
        if os.path.exists(alt):
            path = alt
        else:
            die("no template found (looked for %s)" % TEMPLATE_PATH)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def discover_users():
    if not os.path.isdir(USERS_DIR):
        die("no users/ directory found at %s" % USERS_DIR)
    names = sorted(
        d for d in os.listdir(USERS_DIR)
        if os.path.isfile(os.path.join(USERS_DIR, d, "profile.json")))
    if not names:
        die("no users found under users/ (each needs a profile.json)")
    return names


def load_user(name):
    base = os.path.join(USERS_DIR, name)
    with open(os.path.join(base, "profile.json"), "r", encoding="utf-8") as f:
        profile = json.load(f)
    seen_path = os.path.join(base, "seen-stories.json")
    if os.path.exists(seen_path):
        raw = open(seen_path, "r", encoding="utf-8").read()
        try:
            seen = json.loads(raw)
        except Exception as e:
            log("  seen-stories.json invalid (%s) - salvaging" % e)
            seen = salvage_seen(raw)
    else:
        seen = {"_note": "anti-repetition memory", "stories": []}
    return profile, seen


def now_bangkok():
    if ZoneInfo:
        return datetime.datetime.now(ZoneInfo(TIMEZONE))
    return datetime.datetime.utcnow() + datetime.timedelta(hours=7)


# ------------------------------------------------------------ build queries --

def build_queries(profile):
    q = []
    loc = profile.get("location", {}) or {}
    country = loc.get("country") or ""
    place = loc.get("region") or country
    biz = profile.get("business", {}) or {}
    # Local / business sweep, derived from this person's own profile.
    if country:
        q.append("%s news this week" % country)
    for t in (biz.get("watch_topics") or [])[:5]:
        q.append(t)
    for s in (biz.get("sectors") or [])[:2]:
        q.append(("%s %s business news" % (place, s)) if place else ("%s business news" % s))
    if place:
        q.append("%s infrastructure OR policy development 2026" % place)
    for t in profile.get("intelligence_topics", []):
        term = t.split(" (")[0].split(" — ")[0].strip()
        q.append(term + " breakthrough 2026")
    for c in profile.get("company_watchlist", {}).get("companies", []):
        q.append(c + " new product OR breakthrough announcement")
    for s in profile.get("startup_radar", {}).get("spaces", [])[:3]:
        q.append(s.split(" (")[0].strip() + " startup launch funding 2026")
    weekday = now_bangkok().weekday()
    interests = profile.get("personal_interests", [])
    if interests:
        pick = [interests[(weekday + i) % len(interests)] for i in range(4)]
        q += [p + " latest news 2026" for p in pick]
    for show in profile.get("podcasts", {}).get("shows", []):
        q.append(show.split(" (")[0].strip() + " latest episode")
    seen_q, out = set(), []
    for item in q:
        if item.lower() not in seen_q:
            seen_q.add(item.lower())
            out.append(item)
    return out[:SEARCH_MAX]


# ------------------------------------------------------------- brave search --

def brave_search(query, token, count=5):
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
            out.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "desc": re.sub("<[^>]+>", "", r.get("description", "") or ""),
                "age": r.get("age") or r.get("page_age") or "",
            })
        return out
    return []


def gather_results(queries, token):
    digest = []
    for i, query in enumerate(queries, 1):
        log("[%2d/%d] %s" % (i, len(queries), query))
        hits = brave_search(query, token)
        if hits:
            digest.append({"query": query, "results": hits})
        time.sleep(1.1)
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

Return the briefing as a single JSON object - ONLY the content, no HTML. A \
program renders it into the page, so keep it compact and stop when done. Be \
substantive but disciplined: a few tight sentences per text field, not essays. \
The whole briefing must fit comfortably and you MUST finish with valid closing \
JSON braces - never trail off mid-object.

EDITORIAL RULES
- Respect the profile's attention_budget. End complete, never pad. Empty \
sections/panels are fine - use the "empty" field to say so honestly.
- Cluster duplicate coverage into ONE card per event. Label confidence. Frame \
Opportunities as hypotheses, not predictions.
- Anti-repetition: you are given stories already briefed. Do NOT re-brief one \
unless there is a genuine development; if so, write it as a development.
- Honour learned_preferences, the deprioritise list, the source philosophy, and \
the mandatory Thai/Chiang Mai sweep.
- Only state a fact a provided search result supports. Use REAL source URLs from \
the results. Never invent a URL, statistic, or market figure.
- Text fields may contain light inline HTML: <b>...</b> for emphasis and \
<a href="URL" target="_blank">label</a> for inline links. Nothing else.

JSON SHAPE (omit any field you have no content for; use [] for empty lists):
{
 "lead": "one rich paragraph for the masthead - today's summary, may use <b>",
 "sections": {
   "alerts":        {"cards": [CARD, ...]},
   "opportunities": {"cards": [CARD, ...], "more": [MOREITEM, ...]},
   "major":         {"cards": [CARD, ...], "more": [MOREITEM, ...]}
 },
 "podcasts": [PODCARD, ...],
 "rail": {
   "today":     [{"time":"19 Jul","color":"interest","what":"...","sub":"..."}],
   "companies": {"items":[GEM, ...], "empty":"text if none, else omit"},
   "startups":  {"items":[GEM, ...], "empty":"..."},
   "markets":   {"items":[GEM, ...], "empty":"..."},
   "gems":      {"items":[GEM, ...]},
   "interest":  [{"icon":"🚀","title":"...","body":"... <a ...>src</a>"}]
 },
 "footer": "closing footer paragraph, may use <br> and <b>",
 "seen_add": [{"id":"kebab-slug","first_briefed":"YYYY-MM-DD","section":"major",
               "summary":"one line","status":"active"}],
 "run_summary": "2-3 sentences: what led, what was empty"
}
CARD = {"mode":"AI capability","relevance":"High|Med|Low","confidence":"Primary \
- OpenAI","market":"68% ▲3 (optional)","headline":"...","summary":"optional \
paragraph","wwuw":[{"k":"What","v":"..."},{"k":"Why","v":"..."}],"sources":\
[{"label":"OpenAI","url":"https://..."}]}
PODCARD = {"mode":"Diary of a CEO","confidence":"Show notes","headline":"...",\
"summary":"...","verdict":{"label":"Worth a listen","rel":"Med"},"verdict_conf":\
"Confidence: show-notes based","sources":[{"label":"...","url":"..."}]}
MOREITEM = {"title":"...","body":"paragraph, may include inline <a> sources"}
GEM = {"mode":"Tesla (optional)","accent":"company (optional)","conf":"Company \
posts, 10 Jul","title":"...","body":"paragraph, may include inline <a> sources"}
color/accent values: alert opportunity major company startup market gem interest \
podcast muted.

Output EXACTLY this and nothing else:
===JSON_BEGIN===
{ ...the object... }
===JSON_END==="""


def build_user_prompt(profile, seen, digest_text, dt):
    date_iso = dt.strftime("%Y-%m-%d")
    if os.name != "nt":
        date_human = dt.strftime("%A, %-d %B %Y")
    else:
        date_human = dt.strftime("%A, %d %B %Y")
    return (
        "TODAY is %s (%s), Asia/Bangkok, Chiang Mai.\n\n"
        "=== PROFILE (what is relevant; obey learned_preferences) ===\n%s\n\n"
        "=== ALREADY BRIEFED (anti-repetition memory) ===\n%s\n\n"
        "=== FRESH SEARCH RESULTS (your only sourced facts) ===\n%s\n\n"
        "Now produce today's briefing JSON in the required format."
        % (date_human, date_iso,
           json.dumps(profile, ensure_ascii=False, indent=1),
           json.dumps(seen, ensure_ascii=False)[:12000], digest_text))


# ------------------------------------------------------------ call anthropic --

def generate(profile, seen, digest_text, dt):
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    log("calling Anthropic model %s (streaming) ..." % MODEL)
    parts = []
    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt(
            profile, seen, digest_text, dt)}],
    ) as stream:
        for chunk in stream.text_stream:
            parts.append(chunk)
        final = stream.get_final_message()
    if final.stop_reason == "max_tokens":
        die("model hit max_tokens - refusing to publish. Raise MAX_TOKENS.")
    text = "".join(parts)
    log("received %d chars from model" % len(text))
    return text


def parse_json(raw):
    i = raw.find(JSON_BEGIN)
    j = raw.rfind(JSON_END)
    if i != -1 and j != -1 and j > i:
        blob = raw[i + len(JSON_BEGIN):j].strip()
    else:
        # fall back to the outermost braces
        a, b = raw.find("{"), raw.rfind("}")
        if a == -1 or b == -1 or b < a:
            die("no JSON object found in model output")
        blob = raw[a:b + 1]
    try:
        return json.loads(blob)
    except Exception as e:
        die("model output is not valid JSON: %s" % e)


# --------------------------------------------------------------- rendering ---

def _sources(srcs):
    if not srcs:
        return ""
    links = "".join(
        '<a href="%s" target="_blank">%s</a>\n' % (s.get("url", "#"), s.get("label", "link"))
        for s in srcs)
    return '<div class="sources">%s</div>' % links


def _chip_rel(relevance, label):
    if not relevance:
        return ""
    cls = REL_CLASS.get(relevance, "rel-med")
    return '<span class="chip %s">%s: %s</span>' % (cls, label, relevance)


def render_card(card, accent, wwuw_label, rel_label, is_alert=False):
    pulse = '<span class="pulse"></span>' if is_alert else ""
    chips = ['<span class="chip mode" style="--accent:var(--%s)">%s%s</span>'
             % (accent, pulse, card.get("mode", ""))]
    chips.append(_chip_rel(card.get("relevance"), rel_label))
    if card.get("market"):
        chips.append('<span class="chip mkt">Market: %s</span>' % card["market"])
    if card.get("confidence"):
        chips.append('<span class="chip conf">%s</span>' % card["confidence"])
    parts = ['<div class="card">', '<div class="chips">%s</div>' % "".join(c for c in chips if c)]
    parts.append("<h3>%s</h3>" % card.get("headline", ""))
    if card.get("summary"):
        parts.append('<p class="summary">%s</p>' % card["summary"])
    rows = card.get("wwuw") or []
    if rows:
        rr = "".join('<div class="wwuw-row"><div class="k">%s</div><div class="v">%s</div></div>'
                     % (r.get("k", ""), r.get("v", "")) for r in rows)
        parts.append('<details class="wwuw" open><summary><span class="tri">&#9654;</span> %s</summary>%s</details>'
                     % (wwuw_label, rr))
    parts.append(_sources(card.get("sources")))
    parts.append("</div>")
    return "".join(parts)


def render_podcard(card):
    chips = '<span class="chip mode" style="--accent:var(--podcast)">%s</span>' % card.get("mode", "")
    if card.get("confidence"):
        chips += '<span class="chip conf">%s</span>' % card["confidence"]
    v = card.get("verdict") or {}
    verdict = ""
    if v:
        cls = REL_CLASS.get(v.get("rel"), "rel-med")
        verdict = ('<p class="summary" style="margin-top:8px;">'
                   '<span class="chip %s" style="margin-right:6px;">%s</span>'
                   '<span class="chip conf">%s</span></p>'
                   % (cls, v.get("label", ""), card.get("verdict_conf", "")))
    return ('<div class="card"><div class="chips">%s</div><h3>%s</h3>'
            '<p class="summary">%s</p>%s%s</div>'
            % (chips, card.get("headline", ""), card.get("summary", ""),
               verdict, _sources(card.get("sources"))))


def render_more(items):
    if not items:
        return ""
    n = len(items)
    body = "".join('<div class="more-item"><h4>%s</h4><p>%s</p></div>'
                   % (it.get("title", ""), it.get("body", "")) for it in items)
    return ('<details class="more"><summary><span class="tri">&#9654;</span> '
            'More if you have time <span class="n">%d item%s</span></summary>%s</details>'
            % (n, "" if n == 1 else "s", body))


def render_section(sec_id, h2, note, accent, wwuw_label, rel_label, data):
    cards = data.get("cards") or []
    body = "".join(render_card(c, accent, wwuw_label, rel_label, sec_id == "alerts")
                   for c in cards)
    if not cards and not data.get("more"):
        body = '<div class="empty">Nothing cleared the bar today.</div>'
    body += render_more(data.get("more"))
    return ('<section class="%s" id="%s"><div class="section-head"><div class="dot">'
            '</div><h2>%s</h2><div class="section-note">%s</div></div>%s</section>'
            % (sec_id, sec_id, h2, note, body))


def render_podcasts(cards):
    body = "".join(render_podcard(c) for c in cards) if cards else \
        '<div class="empty">No new episodes since the last brief.</div>'
    return ('<section class="podcasts" id="podcasts"><div class="section-head">'
            '<div class="dot"></div><h2>Podcast Digest</h2>'
            '<div class="section-note">Your shows &mdash; new episodes only</div></div>%s</section>'
            % body)


def render_gem(item):
    chips = []
    if item.get("mode"):
        chips.append('<span class="chip mode" style="--accent:var(--%s)">%s</span>'
                     % (item.get("accent", "gem"), item["mode"]))
    if item.get("conf"):
        chips.append('<span class="chip conf">%s</span>' % item["conf"])
    chip_html = '<div class="chips">%s</div>' % "".join(chips) if chips else ""
    return ('<div class="gem-item">%s<h4>%s</h4><p>%s</p></div>'
            % (chip_html, item.get("title", ""), item.get("body", "")))


def render_gem_panel(panel_id, title, accent, data):
    items = (data or {}).get("items") or []
    if items:
        body = "".join(render_gem(it) for it in items)
    else:
        body = ('<div class="empty" style="margin:0;">%s</div>'
                % (data or {}).get("empty", "Nothing cleared the bar today."))
    dot = ('<div class="dot" style="width:8px;height:8px;border-radius:50%%;'
           'background:var(--%s)"></div>' % accent)
    return ('<div class="panel %s" id="%s"><div class="panel-head">%s<h2>%s</h2></div>%s</div>'
            % (panel_id, panel_id, dot, title, body))


def render_today(items):
    rows = "".join(
        '<div class="today-item"><div class="time">%s</div>'
        '<div class="bar" style="background:var(--%s)"></div>'
        '<div class="what">%s<small>%s</small></div></div>'
        % (it.get("time", ""), it.get("color", "muted"), it.get("what", ""), it.get("sub", ""))
        for it in (items or []))
    return ('<div class="panel"><div class="panel-head"><h2>Today &amp; Ahead</h2></div>%s</div>'
            % rows)


def render_interest(items):
    body = "".join(
        '<div class="interest-item"><div class="interest-row">'
        '<div class="icon-tile" style="background:rgba(157,143,224,.14)">%s</div>'
        '<div><h4>%s</h4><p>%s</p></div></div></div>'
        % (it.get("icon", "&#9733;"), it.get("title", ""), it.get("body", ""))
        for it in (items or []))
    if not body:
        body = '<div class="empty" style="margin:0;">Nothing light to share today.</div>'
    return ('<div class="panel interest" id="interest"><div class="panel-head">'
            '<div class="dot" style="width:8px;height:8px;border-radius:50%%;'
            'background:var(--interest)"></div><h2>Interest &mdash; no analysis needed</h2></div>%s</div>'
            % body)


def render_budget(counts, budget):
    order = [
        ("alert", "Alerts", "%d/%d" % (counts["alert"], budget.get("alerts", 0))),
        ("opportunity", "Opportunities", "%d/%d" % (counts["opportunity"], budget.get("opportunities", 0))),
        ("major", "Major", "%d/%d" % (counts["major"], budget.get("major_developments", 0))),
        ("company", "Company news", "%d" % counts["company"]),
        ("startup", "Startup radar", "%d" % counts["startup"]),
        ("market", "Market signals", "%d moved" % counts["market"]),
        ("gem", "Hidden gems", "%d/%d" % (counts["gem"], budget.get("hidden_gems", 0))),
        ("interest", "Interest", "%d/%d" % (counts["interest"], budget.get("interest_stories", 0))),
        ("podcast", "Podcasts", "%d new" % counts["podcast"]),
    ]
    total = sum(counts.values()) or 1
    track = "".join('<span style="width:%d%%;background:var(--%s)"></span>'
                    % (round(100 * counts[k] / total), k) for k, _, _ in order)
    legend = "".join(
        '<span class="item"><span class="swatch" style="background:var(--%s)"></span>%s <b>%s</b></span>'
        % (k, label, val) for k, label, val in order)
    empties = sum(1 for k in counts if counts[k] == 0)
    status = "&#10003; %d stories &middot; %d sections empty by design" % (sum(counts.values()), empties)
    return ('<div class="budget"><div class="budget-top"><span class="label">Attention budget</span>'
            '<span class="status">%s</span></div><div class="budget-track">%s</div>'
            '<div class="budget-legend">%s</div></div>' % (status, track, legend))


def _sidebar_foot(profile):
    loc = profile.get("location", {}) or {}
    where = ", ".join(x for x in [loc.get("region") or loc.get("city") or "",
                                  loc.get("country") or ""] if x) or "&mdash;"
    sectors = profile.get("business", {}).get("sectors", []) or []
    sect = " &amp; ".join(s.title() for s in sectors) if sectors else "&mdash;"
    interests = profile.get("personal_interests", []) or []
    ints = " &middot; ".join(interests[:4]) if interests else "&mdash;"
    return ('<div class="sidebar-foot"><div class="profile-line"><b>%s</b></div>'
            '<div class="profile-line">%s</div>'
            '<div class="profile-line">%s</div></div>' % (where, sect, ints))




# Editable list fields exposed in the Profile & Settings panel, and the
# GitHub-Issues relay used to apply changes friends submit from their own
# copy of the dashboard (the static page has no backend, so a labelled issue
# is the write mechanism - see apply_pending_profile_issues below).
PROFILE_LIST_PATHS = {
    "personal_interests": ("personal_interests",),
    "intelligence_topics": ("intelligence_topics",),
    "sectors": ("business", "sectors"),
    "watch_topics": ("business", "watch_topics"),
    "companies": ("company_watchlist", "companies"),
    "startup_spaces": ("startup_radar", "spaces"),
    "deprioritize": ("deprioritize",),
}

GOOGLE_SHEET_CSV_URL = os.environ.get("GOOGLE_SHEET_CSV_URL", "")

CHANGE_LINE_RE = re.compile(r'^-\s*(ADD|REMOVE)\s+([a-zA-Z_]+)\s*:\s*"(.*?)"\s*$', re.M)


def _get_list_ref(profile, path):
    node = profile
    for key in path[:-1]:
        node = node.setdefault(key, {})
    return node.setdefault(path[-1], [])


def _profile_lists(profile):
    return {field: list(_get_list_ref(profile, path))
            for field, path in PROFILE_LIST_PATHS.items()}


def parse_changes_block(text):
    return [(a.upper(), f, v) for a, f, v in CHANGE_LINE_RE.findall(text or "")]


def apply_profile_change(name, changes):
    path = os.path.join(USERS_DIR, name, "profile.json")
    if not os.path.isfile(path):
        return ["user '%s' has no profile.json" % name], False
    with open(path, "r", encoding="utf-8") as f:
        profile = json.load(f)
    results = []
    changed = False
    for action, field, value in changes:
        if field not in PROFILE_LIST_PATHS:
            results.append('rejected (unknown field "%s")' % field)
            continue
        lst = _get_list_ref(profile, PROFILE_LIST_PATHS[field])
        if action == "ADD":
            if value in lst:
                results.append('skipped (already present) ADD %s: "%s"' % (field, value))
            else:
                lst.append(value)
                results.append('applied ADD %s: "%s"' % (field, value))
                changed = True
        elif action == "REMOVE":
            if value in lst:
                lst.remove(value)
                results.append('applied REMOVE %s: "%s"' % (field, value))
                changed = True
            else:
                results.append('skipped (not found) REMOVE %s: "%s"' % (field, value))
        else:
            results.append('rejected (unknown action "%s")' % action)
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
            f.write("\n")
    return results, changed


def fetch_pending_sheet_rows(url):
    """GET the published (world-readable, no auth) CSV export of the
    profile-change Google Sheet. Returns data rows only (header stripped),
    or [] on any problem - never raises."""
    if not url:
        return []
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as e:
        log("  could not fetch profile-change sheet: %s" % e)
        return []
    rows = list(csv.reader(io.StringIO(raw)))
    return rows[1:] if len(rows) > 1 else []


def apply_pending_profile_submissions(known_users, csv_url):
    """Best-effort: replay every row of the published Google Sheet
    (Timestamp, User, Changes - one row per Save-button submission) through
    apply_profile_change(). No per-row "already processed" bookkeeping is
    needed: apply_profile_change() is idempotent (adding something already
    present, or removing something already absent, is a no-op), so safely
    replaying the whole sheet every day needs no extra state and no
    write-back credential. Must never raise - a problem here can't be
    allowed to block publishing the day's briefings."""
    try:
        rows = fetch_pending_sheet_rows(csv_url)
        if not rows:
            return
        log("profile-change sheet rows: %d" % len(rows))
        for row in rows:
            if len(row) < 3:
                continue
            user = (row[1] or "").strip()
            changes = parse_changes_block(row[2])
            if not user or not changes:
                continue
            if user not in known_users:
                log("  skipping unknown user '%s' in profile-change sheet" % user)
                continue
            results, changed = apply_profile_change(user, changes)
            if changed:
                log("  applied profile changes for %s: %s" % (user, "; ".join(results)))
    except Exception as e:
        log("WARNING: profile-change sheet processing failed: %s" % e)


def render_page(data, template, dt, profile, user_id):
    style = re.search(r"<style>.*?</style>", template, re.S)
    script = re.search(r"<script>.*?</script>", template, re.S)
    sidebar = re.search(r'<aside class="sidebar">.*?</aside>', template, re.S)
    if not (style and script and sidebar):
        die("template missing <style>, <script> or sidebar")
    style, sidebar = style.group(0), sidebar.group(0)
    sidebar = re.sub(r'<div class="sidebar-foot">.*?</aside>',
                     _sidebar_foot(profile) + "</aside>", sidebar, flags=re.S)
    script = re.sub(r"pi-feedback-\d{4}-\d{2}-\d{2}",
                    "pi-feedback-" + dt.strftime("%Y-%m-%d"), script.group(0))
    profile_lists_json = json.dumps(_profile_lists(profile), ensure_ascii=False)
    new_decl = "var PROFILE_LISTS = %s;" % profile_lists_json
    script, n_sub = re.subn(r"var PROFILE_LISTS\s*=\s*\{.*?\};", new_decl, script, flags=re.S)
    if not n_sub:
        script = script.replace("<script>", "<script>\n" + new_decl, 1)
    new_user_decl = "var PROFILE_USER = %s;" % json.dumps(user_id)
    script, n_user = re.subn(r'var PROFILE_USER\s*=\s*"[^"]*";', new_user_decl, script, flags=re.S)
    if not n_user:
        script = script.replace("<script>", "<script>\n" + new_user_decl, 1)

    sections = data.get("sections") or {}
    rail = data.get("rail") or {}
    podcasts = data.get("podcasts") or []
    counts = {
        "alert": len((sections.get("alerts") or {}).get("cards") or []),
        "opportunity": len((sections.get("opportunities") or {}).get("cards") or []),
        "major": len((sections.get("major") or {}).get("cards") or []),
        "company": len((rail.get("companies") or {}).get("items") or []),
        "startup": len((rail.get("startups") or {}).get("items") or []),
        "market": len((rail.get("markets") or {}).get("items") or []),
        "gem": len((rail.get("gems") or {}).get("items") or []),
        "interest": len(rail.get("interest") or []),
        "podcast": len(podcasts),
    }

    main_sections = "".join(
        render_section(sid, h2, note, accent, wl, rl, sections.get(sid) or {})
        for sid, h2, note, accent, wl, rl in SECTIONS)
    main_sections += render_podcasts(podcasts)

    rail_html = (render_today(rail.get("today"))
                 + render_gem_panel("companies", "Company News", "company", rail.get("companies"))
                 + render_gem_panel("startups", "Startup Radar", "startup", rail.get("startups"))
                 + render_gem_panel("markets", "Market Signals", "market", rail.get("markets"))
                 + render_gem_panel("gems", "Hidden Gems", "gem", rail.get("gems"))
                 + render_interest(rail.get("interest")))

    weekday = dt.strftime("%A")
    if os.name != "nt":
        date_title = dt.strftime("%-d %B %Y")
    else:
        date_title = dt.strftime("%d %B %Y")
    eyebrow = "Personal Intelligence &middot; %s Morning Brief" % weekday
    greet = "Morning, %s" % profile.get("owner_name", "there")
    footer = ('<footer><div><span class="done"><span class="check">&#10003;</span> '
              '%s brief complete.</span></div>%s</footer>'
              % (weekday, data.get("footer", "")))

    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<title>PI &mdash; Daily Briefing &middot; %s</title>\n%s\n</head>\n<body>\n'
        '<div class="app">\n%s\n<div class="main" id="top">\n'
        '<header class="masthead"><div class="eyebrow">%s</div>'
        '<h1>%s</h1><div class="subhead">%s</div>%s</header>\n'
        '<div class="grid"><div class="briefing">%s%s</div>'
        '<aside class="rail">%s</aside></div>\n</div>\n</div>\n%s\n</body>\n</html>\n'
        % (date_title, style, sidebar, eyebrow, greet, data.get("lead", ""),
           render_budget(counts, profile.get("attention_budget", {})),
           main_sections, footer, rail_html, script))


# ------------------------------------------------------------- validate ------

def validate_html(html, dt):
    date_iso = dt.strftime("%Y-%m-%d")
    if not html.lstrip().startswith("<!DOCTYPE html"):
        die("HTML does not start with <!DOCTYPE html>")
    if not html.rstrip().endswith("</html>"):
        die("HTML does not end with </html>")
    if len(html) < MIN_HTML_LEN:
        die("HTML suspiciously short (%d chars)" % len(html))
    for m in REQUIRED_MARKERS:
        if m not in html:
            die("HTML missing required marker: %r" % m)
    if ("pi-feedback-" + date_iso) not in html:
        die("feedback key not stamped with today's date (%s)" % date_iso)
    log("HTML validated: %d chars" % len(html))


def merge_state(seen, add, dt):
    existing = {s.get("id") for s in seen.get("stories", [])}
    for item in add or []:
        if item.get("id") and item["id"] not in existing:
            seen.setdefault("stories", []).append(item)
    cutoff = (dt.date() - datetime.timedelta(days=30)).isoformat()
    seen["stories"] = [
        s for s in seen.get("stories", [])
        if str(s.get("first_briefed", "")) >= cutoff or s.get("status") == "active"
    ]
    return seen


# ----------------------------------------------------------------------- main -

def run_user(name, template, brave, dt):
    log("--- user: %s ---" % name)
    profile, seen = load_user(name)
    queries = build_queries(profile)
    log("  running %d searches" % len(queries))
    digest = gather_results(queries, brave)
    if not digest:
        die("no search results - skipping %s" % name)
    digest_text = results_to_text(digest)

    raw = generate(profile, seen, digest_text, dt)
    data = parse_json(raw)
    html = render_page(data, template, dt, profile, name)
    validate_html(html, dt)
    seen = merge_state(seen, data.get("seen_add"), dt)

    base = os.path.join(USERS_DIR, name)
    date_iso = dt.strftime("%Y-%m-%d")
    for fn in ("dashboard_%s.html" % date_iso, "dashboard_latest.html"):
        with open(os.path.join(base, fn), "w", encoding="utf-8") as f:
            f.write(html)
    with open(os.path.join(base, "seen-stories.json"), "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)
    log("  published %s (%d chars) - %s"
        % (name, len(html), data.get("run_summary") or ""))


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        die("ANTHROPIC_API_KEY not set")
    brave = os.environ.get("BRAVE_API_KEY")
    if not brave:
        die("BRAVE_API_KEY not set")

    dt = now_bangkok()
    log("=== PI briefing run for %s (Asia/Bangkok) ===" % dt.strftime("%Y-%m-%d"))
    template = read_template()
    users = discover_users()
    log("users: %s" % ", ".join(users))

    apply_pending_profile_submissions(users, GOOGLE_SHEET_CSV_URL)

    ok = []
    for name in users:
        try:
            run_user(name, template, brave, dt)
            ok.append(name)
        except Exception as e:
            log("WARNING: user '%s' failed, keeping its previous page: %s" % (name, e))
    if not ok:
        die("all %d user(s) failed - nothing published" % len(users))
    log("=== done: published %d/%d (%s) ===" % (len(ok), len(users), ", ".join(ok)))


if __name__ == "__main__":
    try:
        main()
    except PipelineError as e:
        log("FATAL: %s" % e)
        sys.exit(1)
