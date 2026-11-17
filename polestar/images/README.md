# Images for the Polestar screen

These files stay on the Pi and are ignored by git.

- `logo.png`: shown next to each car's name. A black-on-white or transparent PNG works best.
- `cars/`: one or more car pictures (jpg or png). One is picked at random each time the screen refreshes. Drawings on a white background look best on e-ink.

The bridge converts everything to crisp 1-bit at startup. After adding or changing images, run
`docker compose restart polestar-bridge` from `larapaper/`.
