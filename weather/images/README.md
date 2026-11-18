# Pictures for the weather screen

Pictures of the house for each season and kind of weather go here. They stay on the Pi and are ignored by git, so the house never ends up in the public repo.

When there's at least one picture, the left side of the weather screen shows the one that matches right now, with the current conditions and details underneath. With no pictures, the screen uses its plain layout with a weather icon.

## Names

Name each file for the weather it shows. `.jpg`, `.jpeg`, `.png` and `.webp` all work.

| File | Shown when |
| --- | --- |
| `snowing.jpg` | it's snowing, or snow is likely in the next 2 hours |
| `rain.jpg` | it's raining, drizzling or storming, or rain is very likely within the hour |
| `snow-cover.jpg` | there's an inch or more of snow on the ground and it isn't snowing |
| `windy.jpg` | gusts of 25 mph or more, or wind of 15 mph or more |
| `clear.jpg` | clear or mostly clear |
| `cloudy.jpg` | partly cloudy, cloudy or foggy |
| `spring.jpg` | any dry day in April and May (leafy trees) |
| `summer.jpg` | any dry day from June to September |

`clear`, `cloudy` and `windy` have bare trees, so they cover October to March.

You can add more specific pictures later as `<season>-<weather>.jpg`, for example `summer-rain.jpg` or `winter-clear.jpg`. Seasons are `spring` (Apr–May), `summer` (Jun–Sep), `fall` (Oct–Nov) and `winter` (Dec–Mar). The most specific picture wins. If a picture is missing, the screen uses the closest one: `windy` falls back to `cloudy`, `snow-cover` to `cloudy`, `snowing` to `snow-cover`, and so on.

## Adding or changing pictures

Copy them into this folder on the Pi, for example from your laptop:

```bash
scp *.jpg pi@<pi-ip>:~/reterminal-e1001/weather/images/
```

The bridge notices new or changed files on its next refresh (within 15 minutes). There's no need to restart it.

The bridge trims the outer 3% (for hand-drawn frames), crops to the panel and converts the picture to black and white for e-ink. Wide pictures (about 16:9) work best, with the house in the middle. If the crop cuts off too much at the top or bottom, set `SCENE_FOCUS` on `weather-bridge` (0 keeps the top, 1 keeps the bottom; the default is 0.45).

To check a picture, set `WEATHER_SCENE=rain` (or any other name) in `larapaper/.env`, run `docker compose up -d`, and look at the screen. Set it back to `auto` afterwards.
