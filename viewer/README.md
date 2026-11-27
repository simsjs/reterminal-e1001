# Screen viewer

The E1001's screens on your phone, any time. It shows the latest picture
LaraPaper made for every screen in the display's playlist, in the same order,
and marks the one that is on the display right now. Tap a screen to see it
full size and swipe between them.

It reads LaraPaper's database and picture folder (both mounted read-only).
Each picture shows how long ago it was made; overnight, while the display
sleeps, the pictures are the last ones from before 10 PM.

**Refresh** asks LaraPaper to re-fetch and redraw every screen right away
(once you've added a LaraPaper API token, below). The cards dim and say
"refreshing…" until each new picture is in, usually 5–30 seconds. This only
replaces the pictures: it never changes what's on the display or moves the
rotation on. Without the token, Refresh just re-reads the latest pictures.

## Set up

1. Make a token on the Pi and put it in `larapaper/.env`:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(24))"
   ```

   ```
   VIEWER_TOKEN=<the token>
   ```

2. To let **Refresh** redraw the screens, make a LaraPaper API token: open
   LaraPaper (`http://<pi-ip>:4567`), go to **Settings → API Tokens**, name it
   `viewer`, leave the type as **API**, click **Create token** and copy it
   (it's shown only once). Add it to `larapaper/.env`:

   ```
   LARAPAPER_API_TOKEN=<the LaraPaper token>
   ```

3. Start it:

   ```bash
   cd ~/reterminal-e1001/larapaper
   docker compose up -d --build viewer
   ```

4. On your phone, on the home Wi-Fi, open **once**:

   `http://<pi-ip>:4574/?token=<the token>`

   The viewer remembers your phone for a year and takes the token out of the
   address bar. In Chrome, use **⋮ → Add to Home screen** to get an icon.

## Settings (`larapaper/.env`)

| Setting | What it does |
| --- | --- |
| `VIEWER_TOKEN` | The lock. Empty means anyone on your Wi-Fi can open it. |
| `VIEWER_TITLE` | The name at the top (default `E1001`). |
| `LARAPAPER_API_TOKEN` | A LaraPaper API token, so Refresh redraws the screens. Empty means Refresh only re-reads the pictures. |
| `VIEWER_BIND` | `0.0.0.0` (default) opens port 4574 to the home network; `127.0.0.1` keeps it on the Pi only. |

## Away from home

Port 4574 only answers on your home network. To see the screens from
anywhere, put the Pi and your phone on [Tailscale](https://tailscale.com/) (free
for personal use) and open `http://<pi's tailscale name>:4574/` instead. Don't
forward the port on your router.

## Checking on it

```bash
docker compose ps viewer           # should say "healthy"
curl -s localhost:4574/health      # {"ok": true}
docker compose logs -f viewer
```

If Refresh says "didn't refresh" for a screen, check `docker compose logs app`
for that screen's error. If every screen fails, the log shows
"LaraPaper didn't accept LARAPAPER_API_TOKEN": make a new token and restart
the viewer. A tap within 20 seconds of the last one is ignored, so a
double-tap doesn't redraw everything twice.

Some screens keep their own short caches (the calendar keeps its data for 10
minutes), so a redraw can show the same data if it was fetched moments ago.
