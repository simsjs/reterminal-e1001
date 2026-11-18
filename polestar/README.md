# Polestar status screen

A full-screen LaraPaper recipe for the E1001 showing both cars (Snow and Midnight). It has the same header row as the This Week screen: page dots, **POLESTAR STATUS**, Wi-Fi and battery. Below the header, a car picture fills the left 60% of the screen. On the right, each car gets:

- charge %, range and charge limit
- a **Charging** pill and completion time, shown only while charging
- a charge bar with a limit line
- odometer, efficiency and when the car last reported. Efficiency comes from, in order: the car's **automatic trip** average, then its **manual trip** average, then the bridge's **own calculation**, using the first believable one (8–60 kWh/100 km). A calculated value is marked with "≈". To calculate, the bridge records each stretch of driving between refreshes (distance and battery % used, skipping any that include charging). It then works out efficiency over the last ~500 km using the car's battery size (`POLESTAR_BATTERY_KWH`). It needs about 40 km (25 mi) of driving before the first number appears, and the history is kept in the `polestar` Docker volume. Every time the numbers change, the bridge log lists all of them: `docker compose logs bridges | grep Consumption`.

## How it works

Polestar has no public API, so the Polestar screen is one part of the `bridges` service (`bridges/polestar.py`; `bridges` in `larapaper/docker-compose.yml`, which LaraPaper still reaches as `polestar-bridge`). It uses the community library [pypolestar](https://github.com/pypolestar/pypolestar), the one behind the Home Assistant integration, to log in with your Polestar ID and read each car's data every 10 minutes. It then serves the data to LaraPaper as JSON. It only reads: it never sends commands to the cars and never wakes them. Your Polestar password stays in `larapaper/.env` on the Pi.

## Setup on the Pi

1. **Pull and fill in `.env`.**

   ```bash
   cd ~/reterminal-e1001 && git pull
   cd larapaper
   nano .env   # add the "Polestar status screen" block from .env.example
   ```

   - `POLESTAR_USERNAME` and `POLESTAR_PASSWORD`: your Polestar app login.
   - `POLESTAR_BRIDGE_TOKEN`: make one with `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`.
   - `POLESTAR_CAR_NAMES`: `1234=Snow,5678=Midnight`, using the last 4 characters of each VIN. The first car listed goes on top.
   - For a first look without logging in, set `POLESTAR_DEMO=1`.

2. **Add your images** (they stay out of git):

   ```
   polestar/images/logo.png
   polestar/images/cars/<any names>.jpg   # one is picked at random each refresh
   ```

   From a computer, for example: `scp logo.png pi@<pi-ip>:~/reterminal-e1001/polestar/images/`

3. **Start it.**

   ```bash
   docker compose up -d --build
   docker compose logs -f bridges   # look for "polestar: Logged in; VINs: ..."
   ```

4. **Import the recipe in LaraPaper.** Go to Plugins → Add Recipe → Import Recipe Archive and upload `polestar/polestar-recipe.zip`. You can download it from GitHub. Then open its settings:
   - **Bridge URL:** `http://polestar-bridge:8765`
   - **Bridge token:** the same value as `POLESTAR_BRIDGE_TOKEN`
   - **Units:** Miles
   - **Page dot / Total pages:** leave the defaults. The bridge sends the real values, worked out from `SCREEN_ORDER`.

5. **Add it to a playlist.** On the E1001's device page, add *Polestar* to the **Day** playlist next to *This Week*. The screen will then alternate between the two on each refresh.

## Extras

- **Car images Polestar has of your cars:** `http://localhost:4569/images?token=<POLESTAR_BRIDGE_TOKEN>`
- **Raw data:** `http://localhost:4569/polestar.json?token=<POLESTAR_BRIDGE_TOKEN>`
- **Status:** `http://localhost:4569/health`

After changing images, run `docker compose restart bridges`.

## Settings (`larapaper/.env`)

| Variable | Default | |
| --- | --- | --- |
| `POLESTAR_REFRESH_MINUTES` | 10 | How often the bridge checks Polestar |
| `POLESTAR_VINS` | all | Limit to specific VINs, comma-separated |
| `POLESTAR_DEMO` | 0 | `1` shows two fake cars |

Image layout can be tuned with `POLESTAR_PHOTO_WIDTH`, `POLESTAR_PHOTO_ZOOM`, `POLESTAR_PHOTO_FOCUS`, `POLESTAR_LOGO_SIZE` and `POLESTAR_LOGO_SMALL_SIZE` (add them to the `bridges` service's environment). See the top of `bridges/polestar.py`.

## Notes

- pypolestar is unofficial. If Polestar changes its side, the screen shows "No Polestar data yet" until the library catches up. When that happens, bump the version in `bridges/requirements.txt` and rebuild.
- Fields a car doesn't report are left off the screen. For example, older Polestar 2s may not report the charge limit or charging power.
- The header font loads from the kitchen-board container (which serves the calendar screens), so both screens use the same type.
