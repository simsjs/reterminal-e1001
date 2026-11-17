# Image for the home energy screen

`logo.png` goes here. It stays on the Pi and is ignored by git.

It's shown in the top-right corner of the screen. Any black-on-white or colored PNG works: the bridge trims it and converts it to solid black and white at 54 px tall.

After adding or changing it, run `docker compose restart dominion-bridge` from `larapaper/`. Without a logo, the corner is left empty.
