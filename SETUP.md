# Setup — moving the daily briefing to the cloud

Everything the automated pipeline needs is in this `pi-briefing-repo` folder. The
steps below are the parts only you can do (they involve your accounts, keys, and
GitHub settings). Once done, the briefing regenerates and publishes every day at
6 AM Chiang Mai time with your computer off.

Files in this folder (this is the full repo layout):

```
generate.py                      the pipeline (the "engine")
requirements.txt                 its one dependency
profile.json                     your interests/rules (already yours)
seen-stories.json                anti-repetition memory (repaired — see note)
dashboard_latest.html            yesterday's page, used as today's template
.github/workflows/daily-briefing.yml   the 6 AM schedule + publish
```

## 1. Put these files in your GitHub repo (`dailybrief-git/pi-briefing`)

Your repo currently only holds the published page on the `gh-pages` branch. We
need the source files on the repo's **main** branch.

Easiest route (no command line):
1. Go to `github.com/dailybrief-git/pi-briefing`.
2. If there is no `main` branch yet, use **Add file → Upload files** on the
   default branch.
3. Drag in all the files from this folder. Keep the folder structure — the
   `.github/workflows/daily-briefing.yml` path matters, so upload the
   `.github` folder as-is (GitHub preserves the nested path when you drag the
   folder in).
4. Commit.

> Upload from your own computer (this folder), not from a copy — that guarantees
> GitHub gets the complete files.

## 2. Add your two API keys as secrets

In the repo: **Settings → Secrets and variables → Actions → New repository
secret.** Add two, exactly named:

| Name | Value |
|------|-------|
| `ANTHROPIC_API_KEY` | your **freshly rotated** Anthropic key |
| `BRAVE_API_KEY` | your Brave Search key |

Secrets are encrypted and never shown in logs. This is the safe home for the
keys — not chat, not the code.

Optional: **Variables** tab → add `MODEL` if you ever want to change the writing
model (defaults to `claude-sonnet-5`). Confirm the exact model id in
console.anthropic.com if a run reports an unknown model.

## 3. Profile-save relay (Google Form, one-time setup)

The dashboard's "Profile & Settings" panel lets anyone using the page (you or
a friend you've shared it with) add/remove interests and topics without
needing to talk to Claude. The page is static (GitHub Pages, no backend), so
the Save button needs somewhere public it can write to.

**We tried a GitHub-token-based approach first and it doesn't work** — even
scoped down to "Issues: write" only, GitHub automatically revokes any of its
own tokens it detects exposed in a public repo, regardless of push-protection
settings. That's not configurable; a real GitHub credential can never safely
live in a published page. So instead:

The Save button submits to a **Google Form**, whose submit endpoint is
designed to be publicly POSTable — no credential involved at all. Responses
land in a linked Google Sheet, published read-only as CSV (also no
credential — a "published to web" link is just a public GET). Each day's run
reads that CSV and applies whatever's there to the right person's
`profile.json`. Re-processing the same row twice is harmless (adding
something already there, or removing something already gone, is a no-op),
so there's no "mark as done" bookkeeping and nothing sensitive anywhere in
the loop.

This is already built into `dashboard_latest.html` and `generate.py` for
Anthony's own form. If you ever need to rebuild it (a new form, a different
dashboard fork, etc.):

1. **forms.google.com** → new blank form → exactly two questions, in order:
   "User" (Short answer), "Changes" (Paragraph).
2. Open the live form → ⋮ menu → **Get pre-filled link** → fill in both
   fields with anything → **Get link** → copy it. It contains
   `entry.<ID>=...` for each field — those IDs go into the dashboard
   template's `GOOGLE_FORM_ENTRY_USER` / `GOOGLE_FORM_ENTRY_CHANGES`
   constants, and `/viewform` → `/formResponse` gives the submit URL for
   `GOOGLE_FORM_URL`.
3. Form editor → **Responses** tab → click the green Sheets icon to create a
   linked spreadsheet.
4. Open that spreadsheet → **File → Share → Publish to web** → pick the
   response sheet, format **Comma-separated values (.csv)** → Publish → copy
   the URL (ends `/pub?output=csv`).
5. Repo → **Settings → Secrets and variables → Actions → Variables tab → New
   repository variable** (a **Variable**, not a Secret — this URL isn't
   sensitive, it's already meant to be public), name it exactly
   `GOOGLE_SHEET_CSV_URL`, paste the value.

One caveat inherent to this approach, not a bug: Google's Forms submit
endpoint doesn't send CORS headers, so the page can't actually read back
whether the submission succeeded — the button says "Sent," not "confirmed
saved." If it's genuinely offline it falls back to copying the change to the
clipboard instead, so nothing is silently lost.

## 4. Turn on Actions and check Pages

- **Actions tab:** if it says workflows are disabled, click to enable them.
- **Settings → Pages:** source should be **Deploy from a branch → `gh-pages` /
  root** (this is likely already set — it's how your page is served today). The
  workflow republishes `gh-pages` each run.

## 5. Do a test run

- **Actions tab → "Daily briefing" → Run workflow** (the `workflow_dispatch`
  button). This runs it immediately instead of waiting for 6 AM.
- Watch the run. Green check = success. Open the log if anything is red — the
  script prints a clear `FATAL: ...` line explaining what went wrong (missing
  key, no search results, truncated output, etc.). Nothing publishes unless the
  page passes every integrity check.
- Then open your page: `https://dailybrief-git.github.io/pi-briefing/`. Note the
  Pages CDN can serve the old copy for up to ~10 minutes after a run — give it a
  moment before deciding it didn't work.

## 6. After it works

Nothing. It runs itself at 6 AM daily. Your feedback loop is unchanged: rate
stories on the page, hit **Copy feedback for Claude**, paste into Cowork, and
I'll update `profile.json` in the repo.

---

### Notes

- **Cost:** GitHub Actions free tier covers one run/day. The two APIs run roughly
  a few cents to ~$0.30 per run depending on how long the briefing is — call it
  under ~$10/month combined. Brave's included monthly credit likely covers the
  search side on its own.
- **seen-stories.json was repaired.** The live file had been truncated
  mid-write (this also affected your current Cowork task). I salvaged all 113
  complete entries and dropped one incomplete one. The new script also
  self-heals: if the file is ever truncated again, it recovers the complete
  entries instead of crashing.
- **Safety rails in the script:** it aborts without publishing if a key is
  missing, if no search results come back, if the model output is truncated, or
  if the page is missing its feedback controls — so a bad run leaves yesterday's
  good page up rather than replacing it with a broken one.
- **Retiring the old task:** once this is running, turn off the Cowork
  `daily-intelligence-briefing` scheduled task so they don't both run.
