#!/usr/bin/env python3
"""Pipeline entry point that runs the normal briefing AND emails it.

Why this exists instead of editing generate.py directly: it keeps generate.py
untouched. It imports the existing pipeline and wraps ``push_briefing`` — which
already receives exactly ``(profile, data, dt)`` for each user — so that right
after each brief is pushed to the app, the same structured data is rendered to a
bulletproof HTML email and sent via Resend.

The email send is non-fatal (email_brief.send_brief swallows and logs its own
errors, and skips silently unless RESEND_API_KEY + a recipient are configured),
so the proven GitHub Pages path is never affected.

It also wraps ``run_user`` with a diagnostic that prints the FULL traceback when a
user fails. generate.main() catches per-user errors and logs only str(e); that
hides the file/line of bugs. This wrapper logs the traceback and then re-raises,
so main()'s existing "keep previous page" behaviour is unchanged.

Workflow change: run this instead of generate.py —
    run: python run_pipeline.py
"""

import traceback

import generate

try:
    import email_brief
except Exception as exc:  # noqa: BLE001
    email_brief = None
    generate.log("email_brief import failed, emails disabled: %s" % exc)


def _wrap_push():
    """Make generate.push_briefing also send the email, per user."""
    original = generate.push_briefing

    def push_and_email(profile, data, dt):
        try:
            original(profile, data, dt)
        except Exception as exc:  # noqa: BLE001 - mirror original's own safety
            generate.log("  ingest wrapper caught: %s" % exc)
        if email_brief is not None:
            try:
                email_brief.send_brief(profile, data, dt)
            except Exception as exc:  # noqa: BLE001
                generate.log("  email: send failed: %s" % exc)

    generate.push_briefing = push_and_email


def _wrap_run_user():
    """Print the full traceback when a user's run raises, then re-raise so
    generate.main() handles it exactly as before."""
    original = generate.run_user

    def run_user_traced(*args, **kwargs):
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
