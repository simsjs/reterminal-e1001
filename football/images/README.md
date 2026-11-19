# Club crests for the Premier League screen

Put one picture per club here, named after the club's 3-letter code: `ARS.png`, `LIV.png`, `MCI.png` and so on. They stay on the Pi and are ignored by git.

- PNG works best (a transparent background is fine); WebP, JPG, GIF and BMP work too.
- The bridge trims each picture and turns it into pure black and white: 26 px in the fixture list and 22 px in the table. Colors darker than mid-gray go black and lighter ones go white.
- Simple, high-contrast pictures read best at this size. If a club's light color (sky blue, yellow) disappears, either use a black-and-white version of that picture or raise `FOOTBALL_CREST_THRESHOLD` in `larapaper/.env` (default 150; try 190).
- A club without a picture keeps the plain round badge with its code.
- The league logo (optional) goes left of the **Matchweek** title. `lion_w_text.png` (the lion with *Premier League*) is shown 40 px tall, the full height of the title (`FOOTBALL_WORDMARK_HEIGHT`). Without it, `lion.png` (the lion alone) is shown 30 px tall, the height of the capitals (`FOOTBALL_LION_HEIGHT`). Without either, the title is text only.

The codes come from the fixtures feed and change only when clubs are promoted or relegated. `http://localhost:4572/health` lists, under `football`, the codes that don't have a picture yet.

New or changed pictures show up on the next refresh (every 15 minutes); there's no need to restart anything. After changing `FOOTBALL_CREST_THRESHOLD`, run `docker compose up -d` from `larapaper/`.
