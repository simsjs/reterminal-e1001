# Implementation plan: tidy-up of the E1001 stack

Written 2026-11-18 from a review of this repo at commit `c02c3aa`. It is meant
to be picked up by a Claude Code session running **on the Pi**, in
`~/reterminal-e1001`, which can run Docker and inspect the live setup.

## Goal

Make the stack safer and simpler with the least disruption to the display:

1. Extend the existing weekly backup to cover this project
2. Pin LaraPaper to the version already running
3. Close the open ports
4. Fix the page dots
5. Remove Morning Brief (keep This Week)
6. Merge the four bridges into one service

## Ground rules

- **Do not change the rotation.** Leave LaraPaper's playlists, sleep mode
  (22:00–06:00) and the 15-minute refresh exactly as they are.
- **One step at a time, in the order below.** Each step is its own branch and
  its own commit(s). Finish a step's checks before starting the next.
- **Ask John before** merging a branch into `main`, before anything that needs
  a new sign-in, and before deleting anything outside this repo.
- **Rollback for every step** is `git revert <commit>` followed by
  `cd larapaper && docker compose up -d --build`.
- **Never commit** `.env`, anything under `polestar/images/`,
  `dominion/images/` or `weather/images/` (other than their READMEs), tokens,
  or the Drive backup location. This repo is public.
- **Do not rename the Docker project or volumes.** The project name
  `larapaper` and the volumes `larapaper_database`, `larapaper_storage`,
  `larapaper_dominion`, `larapaper_strava` and `larapaper_polestar` hold live
  data and logins.
- Prefer doing rebuilds **after 22:00**, while the display sleeps. Steps 1–3
  and 5 should not be visible on the display at any hour.
- Keep the repo's existing style: plain-language commit subjects such as
  "Weather screen: …", and READMEs written for a non-developer.

## What the review found (the starting state)

- `larapaper/docker-compose.yml` runs six services: `app` (LaraPaper,
  `ghcr.io/usetrmnl/larapaper:latest`), `kitchen-board` (draws This Week and
  the paused Morning Brief), and four bridges: `polestar-bridge`,
  `dominion-bridge`, `strava-bridge`, `weather-bridge`.
- Ports 4567–4572 are published to the whole home network.
- Five screens are in rotation: This Week (dot 1), Polestar (2), home energy
  (3), fitness (4), weather (5).
- Page dots are set by hand in five places. Recipe defaults disagree:
  Polestar `page_count` 3, Dominion 3, Strava 4, Weather 5; the calendar
  service uses `KB_PAGE_COUNT` (5). The header CSS is copied into
  `kitchen-board/app/templates/_style.css` and each recipe's `shared.liquid`
  under different prefixes (`ps-`, `de-`, `fx-`, `wx-`).
- Morning Brief is paused (`BRIEF_ENABLED=off`) and not in any playlist. It
  has its own Open-Meteo code in `kitchen-board/app/weather.py`, separate
  from `weather/bridge/weather_bridge.py`.
- The four bridges each repeat the same token check, refresh loop, logo and
  picture handling, Dockerfile and health check.
- All four `*-recipe.zip` files match their `recipe/src/` folders.
- A backup already exists: a weekly job writes what looks like a **restic**
  repository to a `piberry-backup` folder on John's Google Drive. Its last
  write seen was Sat 2026-11-14 around 04:00 ET. What it covers is unknown.

## Step 0: look before changing anything

Record the answers in the step's commit message or tell John; do not commit
secrets.

```bash
uname -m && docker --version && docker compose version
cd ~/reterminal-e1001 && git status && git log --oneline -3
cd larapaper && docker compose ps
docker volume ls | grep larapaper
grep -E '^(FONT_BASE_URL|KB_PAGE_COUNT|LOCAL_BIND|BRIEF_ENABLED)=' .env
crontab -l; sudo crontab -l
systemctl list-timers --all | grep -i -E 'restic|backup|rclone'
docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/usetrmnl/larapaper:latest
docker inspect --format '{{json .Config.Labels}}' ghcr.io/usetrmnl/larapaper:latest
```

Also ask John to confirm, from the LaraPaper web page, each recipe's polling
URL and its "Page dot" and "Total pages" values. Those live in LaraPaper's
database, not in this repo.

## Step 1: backups (extend the existing weekly job)

**Why first:** it only adds things, and it is the safety net for every later
step.

1. Find the existing job (Step 0) and read its script. Confirm it is restic,
   where its password lives, and what paths it backs up today. Run
   `restic snapshots` with the job's own settings to confirm it still works.
2. If the job already covers the items below, change nothing and say so.
   Otherwise add them, keeping the weekly schedule (John is happy with
   weekly):
   - the LaraPaper database: take a consistent copy with
     `sqlite3 <db> ".backup <file>"` (or stop `app` for a few seconds during
     the sleep window), rather than copying a live SQLite file
   - the volumes `larapaper_dominion`, `larapaper_strava`,
     `larapaper_polestar` (logins and driving history) and
     `larapaper_storage`
   - `~/reterminal-e1001/larapaper/.env`
   - `polestar/images/`, `dominion/images/`, `weather/images/` (John's own
     pictures, which exist nowhere else)
3. If a helper script is needed, add it to this repo as
   `tools/backup-e1001.sh` with **no secrets and no Drive location in it**;
   it reads those from the existing job's environment.
4. **Check:** run the job once by hand, then restore the newest snapshot into
   a scratch folder (`restic restore latest --target /tmp/restore-test`) and
   confirm the database file, the token files, `.env` and the pictures are
   there. Delete the scratch folder afterwards.

**Rollback:** remove the added paths from the job.

## Step 2: pin LaraPaper

1. From Step 0, find the version tag that matches the running image's digest
   (check the image labels and the tags listed at
   `ghcr.io/usetrmnl/larapaper`). If no tag matches, pin by digest:
   `image: ghcr.io/usetrmnl/larapaper@sha256:…`.
2. Change only the `image:` line of `app` in `larapaper/docker-compose.yml`,
   with a comment saying how to upgrade on purpose.
3. **Check:** `docker compose up -d` should report `app` as unchanged or
   recreate it from the same image; the LaraPaper page loads and the E1001
   is still listed.

## Step 3: close ports

Already written: branch **`close-ports`** (pushed, not merged).

- Ports 4568–4572 bind to `${LOCAL_BIND:-127.0.0.1}`; 4567 stays open because
  the E1001 needs it. LaraPaper reaches the other services by container name.
- `LOCAL_BIND=0.0.0.0` in `.env` reopens them.

**Before merging, check for the README's fallback setup.** If
`FONT_BASE_URL` in `.env` points at the Pi's IP, or any recipe's polling URL
is `http://<pi-ip>:4568/...` instead of `http://kitchen-board:8080/...`,
closing 4568 would break This Week or the header fonts. In that case either
switch those to the container name first and confirm the screen still
renders, or set `LOCAL_BIND=0.0.0.0` and tell John.

**Check after deploying:** from the Pi, `curl -s localhost:4568/healthz`
works; from another machine, `curl -m 3 http://<pi-ip>:4568/healthz` fails
and `http://<pi-ip>:4567` still loads; the next refresh of each screen looks
normal, fonts included.

## Step 4: fix page dots

**Aim:** one place decides the order and count of dots, so adding or removing
a screen never needs five edits again.

1. Add one setting to `larapaper/.env.example` and pass it to every service:
   `SCREEN_ORDER=week,polestar,energy,fitness,weather`.
2. Each service works out its own position and the total from `SCREEN_ORDER`
   and includes `page_number` and `page_count` in the JSON it already serves.
   The calendar service uses it in place of `PAGE_COUNT` / `KB_PAGE_COUNT`.
3. In each recipe's `shared.liquid` header, prefer the JSON values and fall
   back to the existing custom fields, so nothing breaks if a bridge is
   older than its recipe. Correct the stale defaults in each `settings.yml`.
4. Move the header CSS and markup into **one** source file (for example
   `shared/header.liquid`) and add a small script (`tools/build-recipes.sh`)
   that copies it into each recipe and rebuilds the four `*-recipe.zip`
   files. Keep each recipe's class prefix working, or switch all four to one
   prefix in the same commit.
5. **John's part:** re-import the four recipe zips in LaraPaper once. Warn
   him that re-importing may reset a recipe's custom fields (bridge token,
   units), so he should note them first.
6. **Check:** each screen's preview shows five dots with the right one
   filled; then change `SCREEN_ORDER` on a scratch copy and confirm the dots
   follow.

## Step 5: remove Morning Brief, keep This Week

Morning Brief is not in a playlist, so the display should not notice.

1. Delete `kitchen-board/app/templates/brief.html`,
   `kitchen-board/app/weather.py`, `kitchen-board/preview/brief.png`, the
   Morning Brief tiles in `kitchen-board/content/content.yml`, the
   `/brief` and `/api/brief` routes, and the settings `BRIEF_ENABLED`,
   `BRIEF_ONLY_ICS_URLS`, `WEATHER_LAT`/`WEATHER_LON` **from the calendar
   service only** (the weather bridge still uses them).
2. Check what This Week still needs before deleting shared code: read
   `board.py`, `_macros.html` and `week.html` first. If This Week shows any
   weather, keep that part or feed it from the weather bridge.
3. **Keep the service answering to the name `kitchen-board`.** The This Week
   recipe polls `http://kitchen-board:8080/api/week`, and every other recipe
   loads its header font from `http://kitchen-board:8080/fonts/…`. If the
   folder or service is renamed (for example to `calendar`), add
   `kitchen-board` as a network alias in the compose file.
4. Update the READMEs so they no longer mention Morning Brief.
5. **Check:** `localhost:4568/week` renders as before, `/brief` returns 404,
   and the fonts still load on all five screens.

## Step 6: merge the four bridges into one service

The largest step, so it goes last. Do it as **one bridge per commit**, each
deployed and checked before the next. Order: weather (no login), Strava,
Polestar, then Dominion (its tokens are the most fragile).

**Shape:**

- A new `bridges/` folder with one FastAPI app, one Dockerfile and one
  `requirements.txt`. Each screen keeps its own module
  (`weather.py`, `strava.py`, `polestar.py`, `dominion.py`); the repeated
  parts (token check, refresh loop with "keep serving the last good data",
  e-ink picture conversion, health check) move into a shared module.
- Routes keep their current paths: `/weather.json`, `/fitness.json`,
  `/polestar.json`, `/images`, `/images.json`, `/dominion.json`, `/raw.json`,
  `/health`. They already don't collide.
- **Each route keeps its own token** (`WEATHER_BRIDGE_TOKEN`,
  `STRAVA_BRIDGE_TOKEN`, `POLESTAR_BRIDGE_TOKEN`, `DOMINION_BRIDGE_TOKEN`),
  so no recipe setting changes.
- **Network aliases** on the merged service for every old name:
  `weather-bridge`, `strava-bridge`, `polestar-bridge`, `dominion-bridge`,
  all on port 8765. Recipe URLs then keep working untouched.
- **Mount the existing volumes as they are**, at separate paths:
  `dominion:/data/dominion`, `strava:/data/strava`,
  `polestar:/data/polestar`, and point `TOKEN_FILE`, `STATE_DIR` and the
  Dominion `HOME` at them. No data is copied and nobody signs in again.
  The files inside are owned by `nobody`; keep running as `nobody`.
- One failing screen must not take down the others: each module's refresh
  loop catches its own errors, and `/health` reports each screen separately.
- Dominion: keep the 20-minute token keep-alive and the lock that stops it
  overlapping with a data refresh. Update the `dompower login` commands in
  `dominion/README.md` for the new paths.
- As each bridge moves, remove its old service from the compose file in the
  same commit and move its published port (if kept) to the merged service.

**Check after each commit:** the moved screen's raw JSON matches what the old
bridge returned just before (save a copy first and compare, ignoring
timestamps); `/health` is good; the next refresh on the display looks the
same. For Dominion, also confirm an hour later that the token file was
rewritten by the keep-alive.

**Rollback:** revert that one commit and rebuild; the old bridge and its
volume come back exactly as they were.

## Left out on purpose

- Rotation and playlist changes (John asked to leave them alone).
- Splitting the long README and pinning the Python dependencies. Worth doing
  later; not part of this job.

## Done when

- Only port 4567 answers from the home network.
- `docker compose ps` shows three services: LaraPaper, the calendar service
  and the merged bridges.
- All five screens render with correct dots and fonts through a full
  rotation.
- A fresh backup restores cleanly, including logins and pictures.
