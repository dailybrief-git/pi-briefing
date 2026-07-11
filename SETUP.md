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

## 3. Turn on Actions and check Pages

- **Actions tab:** if it says workflows are disabled, click to enable them.
- **Settings → Pages:** source should be **Deploy from a branch → `gh-pages` /
  root** (this is likely already set — it's how your page is served today). The
  workflow republishes `gh-pages` each run.

## 4. Do a test run

- **Actions tab → "Daily briefing" → Run workflow** (the `workflow_dispatch`
  button). This runs it immediately instead of waiting for 6 AM.
- Watch the run. Green check = success. Open the log if anything is red — the
  script prints a clear `FATAL: ...` line explaining what went wrong (missing
  key, no search results, truncated output, etc.). Nothing publishes unless the
  page passes every integrity check.
- Then open your page: `https://dailybrief-git.github.io/pi-briefing/`. Note the
  Pages CDN can serve the old copy for up to ~10 minutes after a run — give it a
  moment before deciding it didn't work.

## 5. After it works

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
