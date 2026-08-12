#!/usr/bin/env python3
"""Pipeline entry point that runs the normal briefing AND emails it.

Keeps generate.py's own logic untouched by wrapping two of its functions:

* ``push_briefing`` -> also renders + sends the per-user email (via email_brief).
* ``run_user``      -> captures the current user's slug (so the email can link to
                       THAT user's dashboard) and prints a full traceback on
                       failure, then re-raises so main()'s handling is unchanged.

Per-user dashboard links: each user is published at
``DASHBOARD_BASE_URL/<slug>/`` by the workflow's publish step, where <slug> is the
users/<slug> folder name == run_user's ``name`` argument. We compute that URL here
and hand it to email_brief.send_brief so friends' emails point at their own page,
not the owner's.

All email work is non-fatal (send_brief swallows/logs its own errors, and skips
unless configured), so the proven GitHub Pages path is never affected.

Workflow: run this instead of generate.py -> ``run: python run_pipeline.py``
Relevant env: DASHBOARD_BASE_URL (e.g. https://dailybrief-git.github.io/pi-briefing),
RESEND_API_KEY, EMAIL_FROM; RESEND_TO optional (single-recipient override).
"""

import os
import traceback

import generate

try:
    import email_brief
except Exception as exc:  # noqa: BLE001
    email_brief = None
    generate.log("email_brief import failed, emails disabled: %s" % exc)

# Holds the slug of the user currently being processed, so the push/email wrapper
# can build that user's dashboard URL.
_STATE = {"slug": None}


def _dashboard_url_for(slug):
    base = (os.environ.get("DASHBOARD_BASE_URL") or "").rstrip("/")
    if base and slug:
        return "%s/%s/" % (base, slug)
    # Fall back to a single fixed URL if a base isn't configured.
    return os.environ.get("DASHBOARD_URL") or None


def _wrap_push():
    """Make generate.push_briefing also send the per-user email."""
    original = generate.push_briefing

    def push_and_email(profile, data, dt):
        try:
            original(profile, data, dt)
        except Exception as exc:  # noqa: BLE001 - mirror original's own safety
            generate.log("  ingest wrapper caught: %s" % exc)
        if email_brief is not None:
            try:
                email_brief.send_brief(
                    profile, data, dt,
                    dashboard_url=_dashboard_url_for(_STATE["slug"]))
            except Exception as exc:  # noqa: BLE001
                generate.log("  email: send failed: %s" % exc)

    generate.push_briefing = push_and_email


def _wrap_run_user():
    """Capture the per-user slug and print full tracebacks on failure."""
    original = generate.run_user

    def run_user_traced(*args, **kwargs):
        _STATE["slug"] = args[0] if args else kwargs.get("name")
        try:
            return original(*args, **kwargs)
        except Exception:
            generate.log("  ---- FULL TRACEBACK (diagnostic) ----")
            for line in traceback.format_exc().rstrip().splitlines():
                generate.log("  " + line)
            generate.log("  ---- END TRACEBACK ----")
            raise

    generate.run_user = run_user_traced


if __name__ == "__main__":
    _wrap_push()
    _wrap_run_user()
    generate.main()
