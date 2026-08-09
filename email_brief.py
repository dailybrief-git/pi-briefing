#!/usr/bin/env python3
"""Render + send the daily brief as a bulletproof HTML email.

Same source of truth as the dashboard: this takes the SAME structured ``data``
dict that ``generate.py`` builds (see JSON SHAPE in generate.py) and produces an
email-safe HTML version — table layout, all critical CSS inlined, no JS / flex /
grid / <details> / background-images, so it renders reliably in Gmail, Apple
Mail, Outlook, etc. It mirrors the dashboard's full content depth; the only
things collapsed to a link are the same things collapsed in the dashboard (the
"more if you have time" extras).

Design reference: ../email_brief_sample.html (the approved static sample).

Portability: ``render_email()`` is pure (data -> HTML string) with no pipeline
dependencies, so the Lovable app can reuse it later; only ``send_brief()`` knows
about Resend.

CLI (for local testing, no API keys needed to render):
    python email_brief.py --data sample_data.json --out /tmp/out.html
    python email_brief.py --data sample_data.json --send    # needs RESEND_API_KEY
"""

import argparse
import datetime
import html as htmllib
import json
import os
import re
import sys
import urllib.error
import urllib.request

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

RESEND_ENDPOINT = "https://api.resend.com/emails"
TIMEZONE = "Asia/Bangkok"
FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif")

# Accent hex — mirrors the dashboard CSS tokens.
ACCENT = {
    "alert": "#e0574a", "opportunity": "#d9a441", "major": "#4a90d9",
    "gem": "#5fb8a8", "interest": "#9d8fe0", "podcast": "#d97ba6",
    "company": "#4fb8d9", "startup": "#a8c95f", "market": "#7f9ec9",
    "good": "#57b98a", "muted": "#7f8ea3",
}
# Tinted chip backgrounds per accent (dark, low-chroma).
CHIP_BG = {
    "alert": "#241419", "opportunity": "#231e12", "major": "#111a28",
    "gem": "#12211a", "interest": "#1a1830", "podcast": "#241722",
    "company": "#0f2028", "startup": "#1a1f10", "market": "#141b26",
}
REL_COLOR = {"High": "#57b98a", "Med": "#d9a441", "Low": "#7f8ea3"}
REL_BG = {"High": "#12211a", "Med": "#231e12", "Low": "#0d131c"}

# Left/right border of the body column, shared by every content row.
_SIDE = "border-left:1px solid #1f2a3a;border-right:1px solid #1f2a3a;"


# --------------------------------------------------------------- text utils --

def _as_topic(x):
    """Coerce a profile list item (string OR {name/note/weight} object) to a
    plain display string, so joins never hit a dict."""
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        for k in ("name", "topic", "label", "text", "title", "value"):
            v = x.get(k)
            if isinstance(v, str) and v.strip():
                return v
        for v in x.values():
            if isinstance(v, str) and v.strip():
                return v
    return str(x)


def _strip_tags(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    return htmllib.unescape(s).strip()


def _trim(s, n):
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


_TRAIL_LINKS = re.compile(r"(?:\s*<a\b[^>]*>.*?</a>)+\s*$", re.I | re.S)
_ONE_LINK = re.compile(r"<a\b[^>]*>(.*?)</a>", re.I | re.S)


def split_trailing_links(body):
    """Return (body_without_trailing_source_links, [label, ...]).

    GEM/interest bodies typically end with one or more inline <a>source</a>
    links. We lift those out so they render as a consistent bottom link, while
    inline links *inside* the prose are left untouched.
    """
    body = (body or "").strip()
    m = _TRAIL_LINKS.search(body)
    if not m:
        return body, []
    labels = [_strip_tags(x) for x in _ONE_LINK.findall(m.group(0))]
    clean = body[: m.start()].rstrip(" —-·.,")
    return clean, [x for x in labels if x]


def _spacer(px):
    return ('<div style="height:%dpx;line-height:%dpx;font-size:0;">&nbsp;</div>'
            % (px, px))


# --------------------------------------------------------- block renderers --

def _row(inner, pad, bg="#0a0f16", radius_bottom=False):
    """One full-width row of the 600px body column."""
    extra = "border-radius:0 0 14px 14px;border-bottom:1px solid #1f2a3a;" if radius_bottom else ""
    return ('<tr><td class="px" bgcolor="%s" style="background:%s;padding:%s;%s%s'
            'font-family:%s;">%s</td></tr>' % (bg, bg, pad, _SIDE, extra, FONT, inner))


def _chip(text, color, bg=None):
    style = ("font-size:10px;font-weight:700;letter-spacing:.05em;"
             "text-transform:uppercase;color:%s;" % color)
    if bg:
        style += "background:%s;border-radius:5px;padding:3px 8px;" % bg
    return '<span style="%s">%s</span>' % (style, text)


def _card_chips(card, accent):
    chips = []
    mode = card.get("mode")
    if mode:
        chips.append(_chip(mode, ACCENT.get(accent, "#7f8ea3"),
                           CHIP_BG.get(accent)))
    rel = card.get("relevance")
    rel_label = card.get("_rel_label", "Relevance")
    if rel:
        chips.append(_chip("%s: %s" % (rel_label, rel),
                           REL_COLOR.get(rel, "#7f8ea3"), REL_BG.get(rel)))
    mkt = card.get("market")
    if mkt:
        chips.append(_chip("Market: %s" % _strip_tags(mkt), "#7f9ec9", "#141b26"))
    conf = card.get("confidence")
    if conf:
        chips.append(_chip(_strip_tags(conf), "#7f8ea3"))
    return "&nbsp;".join(chips)


def _wwuw(rows):
    out = []
    for i, r in enumerate(rows or []):
        k, v = r.get("k", ""), r.get("v", "")
        if not v:
            continue
        color = "#9d8fe0" if i == 0 else "#7f8ea3"
        top = "margin-top:11px;padding-top:11px;border-top:1px solid #1f2a3a;" if i == 0 else "margin-top:10px;"
        out.append(
            '<div style="%s">'
            '<div style="font-size:12px;letter-spacing:.04em;text-transform:uppercase;'
            'color:%s;font-weight:700;margin-bottom:3px;">%s</div>'
            '<div style="font-size:14px;color:#c3cddb;line-height:1.55;">%s</div>'
            '</div>' % (top, color, htmllib.escape(k), v))
    return "".join(out)


def _sources_link(labels, url):
    if labels:
        txt = "Sources: " + " &middot; ".join(labels[:3]) + " &rarr;"
    else:
        txt = "Open in dashboard &rarr;"
    return ('<a href="%s" style="font-size:13px;font-weight:700;color:#6f9bc9;">%s</a>'
            % (url, txt))


def _card_shell(accent, inner):
    hexc = ACCENT.get(accent, "#4a90d9")
    return (
        '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" '
        'border="0" style="border-radius:12px;overflow:hidden;"><tr>'
        '<td width="4" bgcolor="%s" style="width:4px;background:%s;font-size:0;line-height:0;">&nbsp;</td>'
        '<td bgcolor="#121a26" style="background:#121a26;border:1px solid #1f2a3a;'
        'border-left:0;padding:16px 18px;font-family:%s;">%s</td>'
        '</tr></table>' % (hexc, hexc, FONT, inner))


def section_header(accent, title, note=""):
    dot = '<span style="color:%s;font-size:15px;">&#9679;</span>' % ACCENT.get(accent, "#7f8ea3")
    note_html = ('<td align="right" valign="middle" style="font-size:11px;color:#7f8ea3;">%s</td>'
                 % note) if note else ""
    inner = ('<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0"><tr>'
             '<td valign="middle" style="font-size:14px;font-weight:700;color:#e8edf4;letter-spacing:.02em;">'
             '%s&nbsp;&nbsp;%s</td>%s</tr></table>' % (dot, title, note_html))
    return _row(inner, "26px 32px 0")


def full_card(card, accent, sid, dash):
    chips = _card_chips(card, accent)
    head = card.get("headline", "")
    parts = ['<div style="margin-bottom:9px;">%s</div>' % chips if chips else "",
             '<div style="font-size:17px;font-weight:700;color:#e8edf4;line-height:1.4;">%s</div>' % head]
    summary = card.get("summary")
    if summary:
        parts.append(_spacer(9))
        parts.append('<div style="font-size:15px;color:#c3cddb;line-height:1.6;">%s</div>' % summary)
    parts.append(_wwuw(card.get("wwuw")))
    parts.append(_spacer(12))
    labels = [s.get("label", "") for s in (card.get("sources") or []) if s.get("label")]
    parts.append(_sources_link(labels, "%s#%s" % (dash, sid)))
    return _row(_card_shell(accent, "".join(parts)), "11px 32px 0")


def more_link(items, sid, dash):
    items = items or []
    if not items:
        return ""
    titles = "; ".join(_trim(_strip_tags(m.get("title", "")), 60) for m in items[:2])
    n = len(items)
    txt = "+%d more if you have time &mdash; %s &rarr;" % (n, titles)
    inner = '<a href="%s#%s" style="font-size:13px;color:#7f8ea3;font-weight:600;">%s</a>' % (dash, sid, txt)
    return _row(inner, "10px 32px 0")


def podcast_card(card, dash):
    chips = [_chip(card.get("mode", "Podcast"), "#d97ba6", "#241722")]
    verdict = card.get("verdict") or {}
    if verdict.get("label"):
        rel = verdict.get("rel", "Med")
        chips.append(_chip(verdict["label"], REL_COLOR.get(rel, "#d9a441"), REL_BG.get(rel)))
    inner = ['<div style="margin-bottom:8px;">%s</div>' % "&nbsp;".join(chips),
             '<div style="font-size:16px;font-weight:700;color:#e8edf4;line-height:1.4;">%s</div>' % card.get("headline", ""),
             _spacer(8),
             '<div style="font-size:15px;color:#c3cddb;line-height:1.6;">%s</div>' % card.get("summary", ""),
             _spacer(11)]
    labels = [s.get("label", "") for s in (card.get("sources") or []) if s.get("label")]
    inner.append(_sources_link(labels, "%s#podcasts" % dash))
    return _row(_card_shell("podcast", "".join(inner)), "11px 32px 0")


def gem_card(item, accent, sid, dash):
    acc = item.get("accent") or accent
    chips = []
    if item.get("mode"):
        chips.append(_chip(item["mode"], ACCENT.get(acc, "#7f8ea3"), CHIP_BG.get(acc)))
    if item.get("conf"):
        chips.append(_chip(_strip_tags(item["conf"]), "#7f8ea3"))
    clean, labels = split_trailing_links(item.get("body", ""))
    inner = ['<div style="margin-bottom:8px;">%s</div>' % "&nbsp;".join(chips) if chips else "",
             '<div style="font-size:16px;font-weight:700;color:#e8edf4;line-height:1.4;">%s</div>' % item.get("title", ""),
             _spacer(8),
             '<div style="font-size:15px;color:#c3cddb;line-height:1.6;">%s</div>' % clean,
             _spacer(11),
             _sources_link(labels, "%s#%s" % (dash, sid))]
    return _row(_card_shell(acc, "".join(inner)), "11px 32px 0")


def interest_card(item, dash):
    icon = item.get("icon", "⭐")
    clean, labels = split_trailing_links(item.get("body", ""))
    body_col = (
        '<div style="font-size:16px;font-weight:700;color:#e8edf4;line-height:1.4;">%s</div>'
        '%s'
        '<div style="font-size:15px;color:#c3cddb;line-height:1.6;">%s</div>'
        '%s%s'
        % (item.get("title", ""), _spacer(7), clean, _spacer(10),
           _sources_link(labels, "%s#interest" % dash)))
    inner = (
        '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td width="40" valign="top" style="width:40px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td width="32" height="32" align="center" valign="middle" bgcolor="#1a1830" '
        'style="width:32px;height:32px;background:#1a1830;border-radius:8px;font-size:17px;">%s</td>'
        '</tr></table></td>'
        '<td valign="top" style="padding-left:12px;">%s</td></tr></table>'
        % (icon, body_col))
    return _row(_card_shell("interest", inner), "11px 32px 0")


def today_table(items):
    if not items:
        return ""
    rows = []
    for it in items:
        color = ACCENT.get(it.get("color", "muted"), "#7f8ea3")
        border = "border-bottom:1px solid #1f2a3a;" if it is not items[-1] else ""
        sub = (" &mdash; " + it["sub"]) if it.get("sub") else ""
        rows.append(
            '<tr><td style="padding:12px 16px;%s">'
            '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0"><tr>'
            '<td width="86" valign="top" style="width:86px;font-size:12px;font-weight:700;color:%s;">%s</td>'
            '<td valign="top" style="font-size:14px;color:#c3cddb;line-height:1.45;">%s%s</td>'
            '</tr></table></td></tr>' % (border, color, it.get("time", ""), it.get("what", ""), sub))
    header = ('<div style="font-size:14px;font-weight:700;color:#e8edf4;letter-spacing:.02em;margin-bottom:11px;">'
              '<span style="color:#7f8ea3;">&#9679;</span>&nbsp;&nbsp;Today &amp; Ahead</div>')
    table = ('<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" '
             'bgcolor="#0d131c" style="background:#0d131c;border:1px solid #1f2a3a;border-radius:10px;">%s</table>'
             % "".join(rows))
    return _row(header + table, "28px 32px 0")


def quiet_box(entries):
    lines = []
    for title, accent, text in entries:
        lines.append('<div style="font-size:13px;color:#c3cddb;line-height:1.55;">'
                     '<b style="color:%s;">%s &mdash; quiet.</b> %s</div>'
                     % (ACCENT.get(accent, "#7f8ea3"), title, text))
    inner = ('<div style="background:#0d131c;border:1px solid #1f2a3a;border-radius:10px;padding:14px 16px;">'
             + _spacer(10).join(lines) + '</div>')
    return _row(inner, "28px 32px 0")


# ----------------------------------------------------------- shell + assemble --

def _weekday_date(dt):
    return dt.strftime("%A, %-d %B %Y") if os.name != "nt" else dt.strftime("%A, %d %B %Y")


def _short_date(dt):
    return dt.strftime("%a %-d %b") if os.name != "nt" else dt.strftime("%a %d %b")


def subject_line(data, dt):
    sections = data.get("sections") or {}
    head = ""
    for sid in ("alerts", "opportunities", "major"):
        cards = (sections.get(sid) or {}).get("cards") or []
        if cards:
            head = _strip_tags(cards[0].get("headline", ""))
            break
    if not head:
        head = _strip_tags(data.get("lead", "")) or "Your morning brief"
    return "Arun Daily · %s — %s" % (_short_date(dt), _trim(head, 78))


def _attention(data):
    sections = data.get("sections") or {}
    rail = data.get("rail") or {}

    def n(x):
        return len(x or [])
    counts = [
        ("alert", "Alerts", n((sections.get("alerts") or {}).get("cards"))),
        ("opportunity", "Opportunities", n((sections.get("opportunities") or {}).get("cards"))),
        ("major", "Major", n((sections.get("major") or {}).get("cards"))),
        ("gem", "Gems", n((rail.get("gems") or {}).get("items"))),
        ("interest", "Interests", n(rail.get("interest"))),
        ("podcast", "Podcasts", n(data.get("podcasts"))),
        ("company", "Company", n((rail.get("companies") or {}).get("items"))),
    ]
    total = sum(c for _, _, c in counts)
    empties = 0
    for k in ("startups", "markets", "companies"):
        p = rail.get(k) or {}
        if not (p.get("items") or []):
            empties += 1
    spans = "&nbsp; ".join(
        '<span style="color:%s;font-weight:700;">&#9679;</span> %s %d'
        % (ACCENT[acc], label, c) for acc, label, c in counts if c)
    head = ("Today &middot; %d stories" % total) + (
        " &middot; %d sections quiet by design" % empties if empties else "")
    return ('<div style="background:#121a26;border:1px solid #1f2a3a;border-radius:10px;padding:12px 16px;">'
            '<span style="font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#7f8ea3;font-weight:700;">%s</span>'
            '%s'
            '<span style="font-size:13px;color:#c3cddb;line-height:1.9;">%s</span></div>'
            % (head, _spacer(8), spans))


def _profile_bits(profile):
    loc = profile.get("location", {}) or {}
    where = ", ".join(x for x in [loc.get("region") or loc.get("city") or "",
                                  loc.get("country") or ""] if x)
    sectors = (profile.get("business", {}) or {}).get("sectors", []) or []
    sect = " & ".join(_as_topic(s).title() for s in sectors)
    interests = profile.get("personal_interests", []) or []
    ints = ", ".join(_as_topic(i) for i in interests[:4])
    return where, sect, ints


def render_email(data, profile, dt, dashboard_url,
                 settings_url="", unsub_url=""):
    """Pure: structured data -> full email HTML string."""
    dash = (dashboard_url or "").rstrip("/") or "#"
    settings_url = settings_url or dash
    unsub_url = unsub_url or dash
    name = profile.get("owner_name") or "there"
    where, sect, ints = _profile_bits(profile)
    eyebrow = dt.strftime("%A") + " morning brief"

    sections = data.get("sections") or {}
    rail = data.get("rail") or {}

    body = []

    # Alerts
    alerts = (sections.get("alerts") or {}).get("cards") or []
    if alerts:
        body.append(section_header("alert", "Alerts", "Bypasses the normal brief"))
        for c in alerts:
            c = dict(c, _rel_label="Relevance")
            body.append(full_card(c, "alert", "alerts", dash))

    # Opportunities
    opp = sections.get("opportunities") or {}
    if opp.get("cards"):
        body.append(section_header("opportunity", "Your Opportunities", "Hypotheses, not predictions"))
        for c in opp["cards"]:
            body.append(full_card(dict(c, _rel_label="Relevance"), "opportunity", "opportunities", dash))
        body.append(more_link(opp.get("more"), "opportunities", dash))

    # Major
    maj = sections.get("major") or {}
    if maj.get("cards"):
        body.append(section_header("major", "Major Developments", "What / why / uncertain / watch"))
        for c in maj["cards"]:
            body.append(full_card(dict(c, _rel_label="Impact on you"), "major", "major", dash))
        body.append(more_link(maj.get("more"), "major", dash))

    # Today & Ahead
    body.append(today_table(rail.get("today")))

    # Hidden Gems
    gems = (rail.get("gems") or {}).get("items") or []
    if gems:
        body.append(section_header("gem", "Hidden Gems"))
        for it in gems:
            body.append(gem_card(it, "gem", "gems", dash))

    # Interests
    interests = rail.get("interest") or []
    if interests:
        body.append(section_header("interest", "Interests", "No analysis needed"))
        for it in interests:
            body.append(interest_card(it, dash))

    # Podcasts
    pods = data.get("podcasts") or []
    if pods:
        body.append(section_header("podcast", "Podcast Digest", "New episodes only"))
        for c in pods:
            body.append(podcast_card(c, dash))

    # Company / Startup / Market panels
    quiet = []
    for key, title, accent, sid in (
        ("companies", "Company News", "company", "companies"),
        ("startups", "Startup Radar", "startup", "startups"),
        ("markets", "Market Signals", "market", "markets"),
    ):
        p = rail.get(key) or {}
        items = p.get("items") or []
        if items:
            body.append(section_header(accent, title))
            for it in items:
                body.append(gem_card(it, accent, sid, dash))
        elif p.get("empty"):
            quiet.append((title, accent, p["empty"]))
    if quiet:
        body.append(quiet_box(quiet))

    body_html = "".join(x for x in body if x)

    html = (
        SHELL_HEAD
        + MASTHEAD
        + _row('<div style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#7f8ea3;font-weight:700;">%s</div>'
               '<div class="h1" style="font-size:27px;font-weight:700;color:#e8edf4;letter-spacing:-.01em;margin:7px 0 4px;">Morning, %s</div>'
               '<div style="font-size:14px;color:#7f8ea3;line-height:1.5;">%s%s</div>'
               % (eyebrow, htmllib.escape(name), _weekday_date(dt),
                  " &middot; " + where if where else ""), "26px 32px 8px")
        + (_row('<div style="font-size:16px;color:#c3cddb;line-height:1.6;">%s</div>' % data.get("lead", ""),
                "14px 32px 4px") if data.get("lead") else "")
        + _row(CTA_PRIMARY, "20px 32px 8px")
        + _row(_attention(data), "16px 32px 6px")
        + body_html
        + _row(CTA_BOTTOM, "26px 32px 30px")
        + FOOTER % (htmllib.escape(sect or "—"), htmllib.escape(ints or "—"))
        + SHELL_TAIL
    )
    html = (html.replace("{{DASHBOARD_URL}}", dash)
                .replace("{{SETTINGS_URL}}", settings_url)
                .replace("{{UNSUB_URL}}", unsub_url))
    return html


# --------------------------------------------------------------------- send --

def send_brief(profile, data, dt, dashboard_url=None):
    """Non-fatal: render + POST via Resend. Skips silently unless configured.

    Mirrors push_briefing()'s contract — any problem is logged and swallowed so
    the proven Pages path is never blocked.
    """
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        _log("  email: RESEND_API_KEY not set - skipping send")
        return
    # Single-user mode: when RESEND_TO is set, ONLY that address is ever
    # emailed, and briefs for any other user are skipped. This keeps the daily
    # email "just for me" even though the pipeline builds dashboards for several
    # users. To enable per-user emailing later, unset RESEND_TO.
    only_to = (os.environ.get("RESEND_TO") or "").strip()
    owner = (profile.get("email") or "").strip()
    if only_to:
        if owner and owner.lower() != only_to.lower():
            _log("  email: single-user mode (RESEND_TO=%s) - skipping %s" % (only_to, owner))
            return
        to = only_to
    else:
        to = owner
    if not to:
        _log("  email: no recipient (profile.email / RESEND_TO) - skipping send")
        return
    dash = (dashboard_url or profile.get("dashboard_url")
            or os.environ.get("DASHBOARD_URL", ""))
    from_addr = os.environ.get("EMAIL_FROM", "Arun Daily <onboarding@resend.dev>")
    settings_url = os.environ.get("SETTINGS_URL", "")
    unsub_url = os.environ.get("UNSUB_URL", "")
    try:
        html = render_email(data, profile, dt, dash, settings_url, unsub_url)
        payload = json.dumps({
            "from": from_addr,
            "to": [to],
            "subject": subject_line(data, dt),
            "html": html,
        }).encode("utf-8")
        req = urllib.request.Request(
            RESEND_ENDPOINT, data=payload, method="POST",
            headers={"Authorization": "Bearer %s" % api_key,
                     "Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=45)
        resp.read()
        _log("  email: sent to %s - HTTP %s" % (to, resp.status))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:400]
        _log("  email: send failed HTTP %s: %s" % (e.code, body))
    except Exception as e:  # noqa: BLE001
        _log("  email: send failed: %s" % e)


def _log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------- shell constants --

SHELL_HEAD = """<!DOCTYPE html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta name="color-scheme" content="dark light">
<meta name="supported-color-schemes" content="dark light">
<title>Arun Daily — Morning Brief</title>
<!--[if mso]>
<noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
<![endif]-->
<style>
  body,table,td,a{ -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }
  table,td{ mso-table-lspace:0pt; mso-table-rspace:0pt; }
  img{ -ms-interpolation-mode:bicubic; border:0; outline:none; text-decoration:none; }
  a{ text-decoration:none; }
  body{ margin:0; padding:0; width:100%!important; height:100%!important; }
  @media only screen and (max-width:600px){
    .wrap{ width:100%!important; }
    .px{ padding-left:20px!important; padding-right:20px!important; }
    .h1{ font-size:24px!important; }
    .btn a{ display:block!important; }
  }
</style>
</head>
<body style="margin:0;padding:0;background:#05080d;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#05080d" style="background:#05080d;">
  <tr><td align="center" style="padding:24px 12px;">
  <!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
  <table role="presentation" class="wrap" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;">
"""

MASTHEAD = ("""<tr><td class="px" bgcolor="#0d1524" style="background:#0d1524;background:linear-gradient(135deg,#0d1524 0%,#101d31 100%);border-radius:14px 14px 0 0;padding:26px 32px 22px;border:1px solid #1f2a3a;border-bottom:0;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
    <td align="left" valign="middle">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
        <td valign="middle" width="34" align="center" height="34" bgcolor="#4a90d9" style="width:34px;height:34px;background:#4a90d9;background:linear-gradient(135deg,#4a90d9,#5fb8a8);border-radius:9px;font-family:""" + FONT + """;font-size:15px;font-weight:800;color:#0a0f16;">A</td>
        <td valign="middle" style="padding-left:11px;">
          <div style="font-family:""" + FONT + """;font-size:17px;font-weight:700;color:#e8edf4;letter-spacing:.02em;line-height:1.1;">Arun&nbsp;Daily</div>
          <div style="font-family:""" + FONT + """;font-size:11px;color:#7f8ea3;line-height:1.3;margin-top:2px;">Personal intelligence brief</div>
        </td>
      </tr></table>
    </td>
    <td align="right" valign="middle" style="font-family:""" + FONT + """;font-size:12px;">
      <a href="{{DASHBOARD_URL}}" style="color:#6f9bc9;font-weight:600;">Open&nbsp;in&nbsp;browser&nbsp;&rarr;</a>
    </td>
  </tr></table>
</td></tr>
<tr><td bgcolor="#0d1524" style="background:#0d1524;border-left:1px solid #1f2a3a;border-right:1px solid #1f2a3a;padding:0 32px;"><div style="height:3px;line-height:3px;font-size:0;background:linear-gradient(90deg,#e0574a,#d9a441,#4a90d9,#5fb8a8,#9d8fe0);border-radius:2px;">&nbsp;</div></td></tr>
""")

CTA_PRIMARY = ("""<table role="presentation" class="btn" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
  <td align="center" bgcolor="#4a90d9" style="background:#4a90d9;background:linear-gradient(135deg,#4a90d9,#5fb8a8);border-radius:10px;">
    <a href="{{DASHBOARD_URL}}" style="display:inline-block;padding:14px 22px;font-family:""" + FONT + """;font-size:15px;font-weight:700;color:#0a0f16;">Open today&rsquo;s full dashboard &rarr;</a>
  </td></tr></table>""")

CTA_BOTTOM = ("""<table role="presentation" class="btn" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
  <td align="center" bgcolor="#16202e" style="background:#16202e;border:1px solid #28374c;border-radius:10px;">
    <a href="{{DASHBOARD_URL}}" style="display:inline-block;padding:14px 22px;font-family:""" + FONT + """;font-size:15px;font-weight:700;color:#e8edf4;">Open the full dashboard &rarr;</a>
  </td></tr></table>""")

FOOTER = ("""<tr><td class="px" bgcolor="#0d1524" style="background:#0d1524;border-radius:0 0 14px 14px;border:1px solid #1f2a3a;border-top:0;padding:22px 32px 26px;font-family:""" + FONT + """;">
  <div style="font-size:13px;color:#7f8ea3;line-height:1.6;"><b style="color:#c3cddb;">Arun Daily</b> &mdash; built from your profile: %s &middot; %s.</div>
  <div style="height:10px;line-height:10px;font-size:0;">&nbsp;</div>
  <div style="font-size:12px;color:#5a6778;line-height:1.6;">
    <a href="{{DASHBOARD_URL}}" style="color:#6f9bc9;">Open dashboard</a> &nbsp;&middot;&nbsp;
    <a href="{{SETTINGS_URL}}" style="color:#6f9bc9;">Adjust what you see</a> &nbsp;&middot;&nbsp;
    <a href="{{UNSUB_URL}}" style="color:#6f9bc9;">Pause emails</a>
  </div>
</td></tr>""")

SHELL_TAIL = """
  </table>
  <!--[if mso]></td></tr></table><![endif]-->
  </td></tr>
</table>
</body>
</html>"""


# ---------------------------------------------------------------------- CLI --

def _now():
    if ZoneInfo:
        return datetime.datetime.now(ZoneInfo(TIMEZONE))
    return datetime.datetime.utcnow() + datetime.timedelta(hours=7)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render/send the daily brief email.")
    ap.add_argument("--data", help="path to a structured brief JSON (the generate.py 'data' object)")
    ap.add_argument("--profile", help="path to a user profile.json", default="users/anthony/profile.json")
    ap.add_argument("--out", help="write rendered HTML here")
    ap.add_argument("--dashboard-url", default=os.environ.get("DASHBOARD_URL", "https://example.github.io/pi/"))
    ap.add_argument("--send", action="store_true", help="also send via Resend (needs RESEND_API_KEY)")
    args = ap.parse_args(argv)

    if not args.data:
        ap.error("--data is required")
    with open(args.data, "r", encoding="utf-8") as f:
        data = json.load(f)
    profile = {}
    if args.profile and os.path.exists(args.profile):
        with open(args.profile, "r", encoding="utf-8") as f:
            profile = json.load(f)

    dt = _now()
    html = render_email(data, profile, dt, args.dashboard_url)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(html)
        print("wrote %s (%d bytes)" % (args.out, len(html.encode("utf-8"))))
    else:
        sys.stdout.write(html)

    if args.send:
        send_brief(profile, data, dt, args.dashboard_url)


if __name__ == "__main__":
    main()
