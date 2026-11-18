# reTerminal E1001 screens

Screens for a Seeed reTerminal E1001 running TRMNL firmware, served by
[LaraPaper](https://github.com/usetrmnl/larapaper) on a Raspberry Pi: a calendar
screen (This Week), a Polestar status screen (see [`polestar/`](polestar/README.md)), a
home energy screen for Dominion Energy (see [`dominion/`](dominion/README.md)), a
fitness screen from Strava (see [`strava/`](strava/README.md)) and a weather screen
(see [`weather/`](weather/README.md)).

The E1001 refreshes every 15 minutes and shows the next screen each time, in
this order (the dots in each screen's header follow the same order):

| Dot | Screen | What it shows |
| --- | --- | --- |
| 1 | **This Week** | yesterday, today and the next three days, a holiday countdown, reminders |
| 2 | **Polestar** | charge, range and charging status of both cars |
| 3 | **Home energy** | this billing cycle's use and cost, yesterday's half-hour readings, the last 7 days |
| 4 | **Fitness** | your latest Strava activity and hours per week |
| 5 | **Weather** | now, the next 5 days, wind, humidity, UV, air quality and sunrise, with a picture of the house that matches the weather |

From 10:00 PM to 6:00 AM the display sleeps, with no refreshes, to save battery.

![This week](kitchen-board/preview/week.png)

## How it fits together

- `kitchen-board/` is a small Python service for the This Week screen. It
  reads calendars from private iCal links, holidays from Google's public US
  holiday calendar, reminders from Google Tasks, and personal countdowns from
  `content/content.yml`. It renders the screen's HTML and serves it as JSON at
  `/api/week`. It also serves the header fonts for every screen.
- `larapaper/docker-compose.yml` runs three services together: LaraPaper, the
  calendar screen (`kitchen-board`) and `bridges`.
- `bridges/` is one Python service for the screens that need outside data. Each
  screen is its own module there (`weather.py`, `strava.py`, `polestar.py` and
  `dominion.py`) with its own routes, token and refresh loop; one screen
  failing doesn't stop the others.
  `http://localhost:4572/health` shows each screen's status. LaraPaper still
  reaches each screen by its old name (`weather-bridge`, `strava-bridge`,
  `polestar-bridge`, `dominion-bridge`), so the recipes never changed.
- `polestar/` holds the Polestar status screen's LaraPaper recipe and pictures.
  Its data (both cars, read from Polestar) comes from `bridges/polestar.py`.
  Setup is in [`polestar/README.md`](polestar/README.md).
- `dominion/` holds the home energy screen's LaraPaper recipe and logo. Its
  data (your Dominion Energy smart-meter readings) comes from
  `bridges/dominion.py`. Setup is in
  [`dominion/README.md`](dominion/README.md).
- `strava/` holds the fitness screen's LaraPaper recipe and sign-in tool. Its
  data (your activities from the Strava API) comes from `bridges/strava.py`.
  Setup is in [`strava/README.md`](strava/README.md).
- `weather/` holds the weather screen's LaraPaper recipe and pictures. Its data
  (forecast from Open-Meteo, air quality, and National Weather Service alerts)
  comes from `bridges/weather.py`. Setup is in
  [`weather/README.md`](weather/README.md).
- `shared/` holds the header row every screen shares (page dots, title, Wi-Fi
  and battery); see "Page dots" below. `tools/build-recipes.sh` copies it into
  the recipes.
- In LaraPaper, one recipe per screen polls that JSON. For This Week, the markup
  in `kitchen-board/recipes/kitchen-board.liquid` fills in the battery and
  Wi-Fi values. LaraPaper's playlist and sleep mode handle the schedule.

## First-time setup on the Pi

1. **Move LaraPaper into this repo.** The compose project is still named
   `larapaper`, so your existing data volumes are reused.

   ```bash
   cd ~/larapaper && docker compose down          # stops LaraPaper; volumes stay
   cd ~ && git clone https://github.com/simsjs/reterminal-e1001.git
   cd ~/reterminal-e1001/larapaper
   cp .env.example .env
   nano .env        # APP_KEY from ~/larapaper/docker-compose.yml, and APP_URL
   docker compose up -d --build
   docker volume ls | grep larapaper    # larapaper_database and larapaper_storage
   ```

   Log in to LaraPaper. Your account and the E1001 should still be there.

2. **Check the preview** at `http://localhost:4568/week` (see "Previews and
   raw data" below for how to open it from your laptop).

3. **Set up LaraPaper.**
   - Set your profile timezone to **America/New_York**. Playlists and sleep
     mode use it.
   - Create a recipe called **This Week** with the polling URL
     `http://kitchen-board:8080/api/week`. Use the **Polling** data strategy
     and the **Liquid** markup language. Paste `kitchen-board/recipes/kitchen-board.liquid` as the
     full-screen markup and refresh every 15 minutes. Turn on "no screen
     padding" if that option appears.
   - Import the other four recipes from their zip files. Each folder's README
     has the steps and the settings to fill in:
     [`polestar/`](polestar/README.md), [`dominion/`](dominion/README.md),
     [`strava/`](strava/README.md) and [`weather/`](weather/README.md).
   - On the E1001's device page, add a playlist (ours is called **Kitchen**)
     with the five screens in the order of the table above, active all day.
   - Turn on **sleep mode** from 22:00 to 06:00.
   - Set the refresh rate to **900 seconds** (15 minutes).

## Connecting calendars

Put private iCal links in `larapaper/.env`, comma-separated, then run
`docker compose up -d`:

- `CALENDAR_ICS_URLS` feed This Week. For Google Calendar, use Settings →
  your calendar → *Secret address in iCal format*.

Until a link is set, This Week shows a sample week, marked
**SAMPLE CALENDAR** in the bottom-right corner.

## Reminders from Google Tasks

The reminders strip on This Week shows your open Google Tasks (the "Tasks"
entry in Google Calendar's sidebar) that are overdue or due in the next 7 days,
soonest first, each with a day label: OVERDUE, TODAY, TMRW or the weekday.
Tick a task off on your phone and it drops off within about 10 minutes.

Google Tasks isn't a real calendar and has no iCal link, so the Pi reads it
through the Google Tasks API. That needs a one-time setup:

1. **Make a Google Cloud project.** Go to
   [console.cloud.google.com](https://console.cloud.google.com), create a
   project (any name, e.g. *E1001*), then open *APIs & Services → Library*,
   search for **Google Tasks API** and click **Enable**.
2. **Set up the consent screen.** In *APIs & Services → OAuth consent screen*
   (called *Google Auth Platform* in newer consoles), choose **External**, fill
   in the app name and your email, and add yourself as a test user. Then go to
   *Audience* (or *Publishing status*) and click **Publish app** so it's
   **In production**. Skip this and Google cancels the sign-in every 7 days.
   You don't need to submit it for verification.
3. **Make a client.** In *Credentials* (or *Clients*), create an **OAuth
   client ID** of type **Desktop app**. Copy the client ID and client secret.
4. **Sign in once, on your laptop:**

   ```bash
   python3 kitchen-board/tools/get_tasks_token.py CLIENT_ID CLIENT_SECRET
   ```

   No Python on your laptop? SSH into the Pi as usual and run the same command
   there. Open the link it prints in your laptop's browser and allow access.
   The browser then shows an error page ("This site can't be reached"); copy
   that page's whole address and paste it into the terminal.

   Sign in with the account that has your Tasks. Google warns that it hasn't
   verified the app, because it's your own. Click *Advanced → Go to E1001*,
   then allow read-only access to Tasks. The script prints three lines.
5. **Paste those three lines into `larapaper/.env` on the Pi**, then run
   `docker compose up -d`.

Optional settings in `.env`: `TASKS_LISTS` limits which task lists are shown
(by name), `TASKS_DAYS_AHEAD` changes the 7-day window, and
`TASKS_INCLUDE_UNDATED=on` adds tasks with no due date. Until Google Tasks is
set up, the strip shows the `reminders:` list in `content.yml`.

## Holiday countdown

The countdown at the top right of This Week shows the next holiday within 60
days from Google's public **Holidays in United States** calendar, so it never
needs updating. You can add your own dates (birthdays, trips) to `countdowns:`
in `content.yml`; whichever is sooner is shown. `HOLIDAY_SHOW_WITHIN_DAYS` in
`.env` changes the 60-day window.

## Page dots

The dots at the top left of every screen come from one setting in
`larapaper/.env`, listing the screens in playlist order:

```
SCREEN_ORDER=week,polestar,energy,fitness,weather
```

Each service works out its own dot and the total from it. After adding,
removing or reordering a screen, change this line and run
`docker compose up -d`; the dots follow on each screen's next refresh. The
recipes' **Page dot** and **Total pages** settings are only a fallback.

The header itself (dots, title, Wi-Fi and battery) is drawn from one file,
`shared/header.css`, with the recipe part in `shared/header.liquid`. After
changing either, run `tools/build-recipes.sh`. It copies them into the four
recipes and the calendar service and rebuilds the recipe zips; then import the
zips in LaraPaper again and rebuild the calendar service.

## Previews and raw data

Only LaraPaper (port 4567) is open to your home network. The previews and the
bridges' raw-data pages (ports 4568-4572) answer on the Pi itself. To open them
in a browser on your laptop, tunnel them over SSH and leave that window open:

```bash
ssh -L 4568:localhost:4568 -L 4569:localhost:4569 -L 4570:localhost:4570 \
    -L 4571:localhost:4571 -L 4572:localhost:4572 pi@<pi-ip>
```

Then use these addresses (the bridge pages need the screen's token from
`.env`, added as `?token=...`):

| Address | What it is |
| --- | --- |
| `http://localhost:4568/week` | This Week preview |
| `http://localhost:4569/images` | Polestar's official pictures of your cars |
| `http://localhost:4570/dominion.json` | home energy raw data (`/raw.json` is Dominion's untouched answer) |
| `http://localhost:4571/fitness.json` | fitness raw data |
| `http://localhost:4572/weather.json` | weather raw data |
| `http://localhost:4572/health` | the status of all four bridge screens (no token needed) |

Ports 4569–4572 all reach the same `bridges` service; each keeps its own port
so old bookmarks still work. To open the ports to the whole network again, set
`LOCAL_BIND=0.0.0.0` in `larapaper/.env` and run `docker compose up -d`.

## Checking on things

From `~/reterminal-e1001/larapaper` on the Pi:

```bash
docker compose ps                    # app, kitchen-board and bridges should say "healthy"
curl -s localhost:4572/health        # each bridge screen: ok, last update, any error
docker compose logs -f bridges       # lines start with weather:, fitness:, polestar: or energy:
docker compose logs -f kitchen-board
```

A screen whose source is down keeps showing its last good data. One screen
failing never stops the others.

## Backups

The whole Pi, including LaraPaper's database, the screens' logins and history
(the Docker volumes), `larapaper/.env` and your own pictures in the `images/`
folders, is backed up every Saturday at 4:00 AM to a NAS on the home network.
The backup is encrypted with restic. LaraPaper is stopped for a few seconds
while its database is copied, during the display's sleep.

- If the NAS can't be reached, the backup tries again 24 hours later, and
  every day after that until it works.
- Run one now: `sudo systemctl start piberry-backup.service`
- See the last run: `journalctl -u piberry-backup.service -n 20`

The backup job lives outside this repo (`/usr/local/sbin/piberry-backup`), and
its password and NAS login stay on the Pi, out of git.

## LaraPaper version

LaraPaper is pinned to a fixed release (0.47.0) in
`larapaper/docker-compose.yml`, so a rebuild never pulls a new version by
surprise. To upgrade on purpose, follow the comment above its `image:` line.
Run a backup first.

## Updating

```bash
cd ~/reterminal-e1001 && git pull
cd larapaper && docker compose up -d --build
```

LaraPaper keeps its own copy of each recipe, so a change to a recipe's
`recipe/src/` files only shows once that recipe's zip is imported again (note
its settings first; re-importing can reset them).

## Notes and troubleshooting

- **The recipe says the data isn't available, but the preview works:** check
  the polling URL. If LaraPaper won't fetch from the container name, set
  `LOCAL_BIND=0.0.0.0` in `.env`, run `docker compose up -d`, and use
  `http://<pi-ip>:4568/api/week` instead.
- **HTML tags show up as text:** switch the recipe's markup language to Blade
  and paste `kitchen-board/recipes/kitchen-board.blade.php` instead.
- **Fonts look generic:** LaraPaper's renderer can't reach the fonts, which
  the `kitchen-board` service serves. For This Week, set
  `FONT_BASE_URL=http://<pi-ip>:4568` and `LOCAL_BIND=0.0.0.0` in `.env` and run
  `docker compose up -d`. For the other four screens, set the same address as
  the recipe's **Header font source** setting.
- **Run This Week on a laptop:**

  ```bash
  cd kitchen-board/app
  pip install -r ../requirements.txt
  SAMPLE_DATA=on FONT_BASE_URL=http://localhost:8080 NOW=2026-11-12T06:02 python server.py
  ```

- **Run the bridges on a laptop** with made-up data, no logins needed:

  ```bash
  cd bridges
  pip install -r requirements.txt
  WEATHER_DEMO=1 STRAVA_DEMO=1 POLESTAR_DEMO=1 DOMINION_DEMO=1 python app.py
  ```

  Then open `http://localhost:8765/weather.json`, `/fitness.json`,
  `/polestar.json` and `/dominion.json`.

Space Grotesk and Space Mono are licensed under the SIL Open Font License 1.1
(see `kitchen-board/app/fonts/`).
