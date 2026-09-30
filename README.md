# Forest Study Group Bot — Cross-Batch Edition

Automates focus-session tracking for a Telegram group across medical
batches (Med22–Med27): manual logging (`/log 90`) or screenshot logging
(forward a Forest completion screenshot, bot OCR's the timer), streaks,
tiers, milestone shoutouts, per-batch leaderboards, a flexible daily
schedule announcement, and scheduled posts (daily total, daily "which
tree" poll, weekly leaderboard, monthly recap).

Start with just your own batch (e.g. Med25) — other batches can be added
any time later with zero code changes, since batch is just something
each person sets for themselves with `/register`.

## 1. Create the bot on Telegram

1. Open Telegram, message **@BotFather**
2. Send `/newbot`, follow the prompts (choose a name + username)
3. BotFather gives you a **token** like `123456:ABC-DEF...` — save it

## 2. Get your group's chat ID

1. Add your new bot to the group
2. Send any message in the group
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
   (replace `<YOUR_TOKEN>`)
4. Look for `"chat":{"id":-100XXXXXXXXXX` — that negative number is your
   `GROUP_CHAT_ID`

## 3. Deploy on Railway (free, no server management)

1. Create a free account at [railway.app](https://railway.app)
2. Push this folder to a new GitHub repo (or use Railway's "Deploy from
   local folder" option if offered)
3. In Railway: **New Project → Deploy from GitHub repo**
4. Under **Variables**, add:
   - `BOT_TOKEN` = your token from step 1
   - `GROUP_CHAT_ID` = your chat ID from step 2
   - `TZ_OFFSET_HOURS` = `3` (Saudi Arabia; adjust if needed)
   - `MANAGER_IDS` = the Telegram user IDs of the 1-2 people allowed to
     post the daily schedule, comma-separated (e.g. `111111111,222222222`).
     Get a user's ID by having them message **@userinfobot** on Telegram.
5. Railway auto-detects Python and installs `requirements.txt`. Set the
   **Start Command** to:
   ```
   python forest_bot.py
   ```
6. Deploy. The bot goes live and stays online continuously.

### Enabling screenshot OCR on Railway

Add a file named `nixpacks.toml` in the repo root with:
```toml
[phases.setup]
nixPkgs = ["tesseract"]
```
This installs the OCR engine Railway needs. Without this file, `/log`
still works — only screenshot-reading is disabled.

## 4. Logging is screenshot-only, by design

Manual `/log 90` is disabled for regular members on purpose — anyone could
type any number, which would wreck trust in the leaderboard fast. Instead,
**two kinds of Forest screenshot both work**, and the bot tells them apart
automatically:

- **A single Timeline entry** ("Successfully planted a 75-minute Apple
  Tree") — adds to that day's total, same as before
- **The daily "Focus Statistics" card** (Overview → day, with the Share
  button) — this one shows a cumulative total for the whole day, so
  logging it **replaces** that day's total rather than adding to it. If
  someone sends both a Timeline shot and later the daily card for the
  same day, the card wins — it's already the authoritative total, so
  nothing double-counts. Sending the card again later (updated total) just
  revises the same day, it doesn't add on top.

This means people can log however suits them — after every session, or
just once at the end of the day with the daily card — without ever
double-counting.

**Anti-cheat, on every submission:**
- **Duplicate check** — every image is hashed; the exact same screenshot
  can never be logged twice, even by different people
- **Date check** — the daily card stamps an exact date (e.g. `09.27
  2026`), so a mismatch there is a hard block. A single Timeline entry
  only shows a relative date heading, so that check is a softer
  heuristic and won't hard-block if it simply can't find a date at all

First-time senders get asked which batch they're in via buttons — no
`/register` command needed, registration happens automatically off the
first screenshot.

## 5. Commands for your members

| Command | What it does |
|---|---|
| `/register Med25` | Optional — register ahead of time instead of waiting for your first screenshot |
| `/setname الاسم` | Optional — override your displayed name (defaults to your Telegram name) |
| `/stats` | Shows your total, tier, and current streak |
| `/leaderboard` | This week's combined ranking across all registered batches |
| `/leaderboard Med25` | This week's ranking for one batch only |
| `/findpartner رياضيات` | Finds someone who logged the same subject tag this week, for a study-buddy match |
| *(forward a screenshot)* | The only way to log a session — see above |

The first 15 people to register in each batch get a permanent **🏅 founder badge** next to their name everywhere — automatic, no command needed.

**Managers only:**

| Command | What it does |
|---|---|
| `/setschedule 13:00 22:00` | Posts and pins today's flexible study window (use this instead of a fixed schedule — post it fresh each morning based on that day's lecture times) |
| `/exammode on` / `/exammode off` | Toggles a gentler mode during exam weeks — shows a banner on the website and reframes the tone away from pure competition |
| `/log 90` *(reply to the student's message)* | Manual correction — the one exception to screenshot-only logging, for when OCR genuinely fails on a legitimate photo |

## 5. What posts automatically (no admin action needed)

- **23:00 daily** — total group minutes for the day
- **17:00 daily** — "which tree did you plant today?" poll
- **Saturday 23:30** — weekly leaderboard (combined + top 3 per active batch) + most-improved member
- **1st of month, 09:00** — monthly recap (total hours, busiest day)
- **Instantly** — milestone shoutout the moment someone crosses 10h / 25h / 50h / 100h
- **Every Saturday** — that week's #1 is permanently recorded into the **Hall of Fame**, shown on the website forever (even after someone else overtakes them later), plus a **batch-vs-batch** post showing both raw totals (so a batch behind has a real reason to recruit) and per-capita averages (so a small, dedicated batch can still win on merit)

The only manual action needed day-to-day is `/setschedule` from a
manager each morning — everything else is fully automatic.

## 7. Public leaderboard website (free, no server)

The `website/` folder is a self-contained page (`index.html` + `data.json`)
that the bot keeps updated automatically. It shows daily/weekly/monthly/
all-time leaderboards, with tabs and a per-batch filter, and needs no
hosting cost. It also renders the top 8 weekly performers as an actual
growing forest (tree height scales with their hours), and spotlights the
week's most-improved member.

You can see it live right now, populated with sample data, here:
https://claude.ai/artifact/DoPWPbMhjkuTCHPcMvbvYy — that's a preview only
(sample names, not connected to your bot); the real `website/index.html`
in this folder is the one to actually deploy to GitHub Pages.

**One-time setup:**

1. Push this whole `forest_bot` folder (bot code *and* `website/` folder)
   to a GitHub repo — public or private both work.
2. In the repo: **Settings → Pages → Source** → set to your main branch,
   folder `/website`. GitHub gives you a free link like
   `https://yourname.github.io/study-leaderboard/`.
3. Create a **GitHub personal access token**: GitHub profile → Settings →
   Developer settings → Personal access tokens → Fine-grained token →
   grant it **read/write access to Contents** for this one repo only.
4. Add these variables alongside your bot's other ones (on Railway, or
   wherever you host it):
   - `GITHUB_TOKEN` = the token from step 3
   - `GITHUB_REPO` = `yourname/study-leaderboard` (repo name)
   - `GITHUB_DATA_PATH` = `website/data.json`

That's it — every time someone logs a session, the bot pushes a fresh
`data.json` to your repo, and the public page picks it up automatically
(GitHub Pages redeploys the *static* files once at setup; only the data
file changes after that, and the page fetches it live on every visit —
no redeploy needed per update). There's also a 30-minute safety-net sync
in case any single push fails.

If you skip the `GITHUB_*` variables entirely, the bot works exactly the
same — the website simply stays unused.

## Growing beyond one batch

Nothing structural needs to change to add Med22, 23, 24, 26, or 27 later
— when someone from another batch joins the group, they just run
`/register Med24` (etc.) themselves. The weekly post automatically starts
showing a breakdown for any batch with active members, with no
reconfiguration needed on your end.

If it grows into something bigger, a free public leaderboard webpage
(hosted at no cost via GitHub Pages) is a natural next step — worth
revisiting once there's a real multi-batch community to show off.

All times are adjustable via the `TZ_OFFSET_HOURS` variable and the
`run_daily(...)` lines near the bottom of `forest_bot.py`.

## 6. Local testing (optional, before deploying)

```bash
pip install -r requirements.txt
export BOT_TOKEN="your_token_here"
export GROUP_CHAT_ID="-100xxxxxxxxxx"
python forest_bot.py
```

## Notes

- Data is stored in a local SQLite file (`forest.db`). On Railway this
  persists as long as you don't delete the deployment; for guaranteed
  durability across redeploys, consider Railway's persistent volume
  add-on.
- OCR accuracy depends on screenshot clarity — the confirm/edit buttons
  exist specifically so a misread number never corrupts the leaderboard.
- Tiers (بذرة → شتلة → شجرة صغيرة → شجرة → غابة) and milestone hour
  thresholds are easy to tweak at the top of `forest_bot.py`.
