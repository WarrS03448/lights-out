# Rank badge art - source

The 25 badges in `hub/webui/static/ranksprite.js` and `ranks.svg` are generated from here.

Each rank says its name, and each grows through its three divisions in its own way (division 1 is
the bare plate, division 2 is clearly wider, division 3 is wider and taller). Rookie: dog tags and
stitched cloth. Private: a chevron and sleeve stripes. Soldier: a helmet and crossed rifles.
Veteran: a star medal and laurels. Operator: night-vision goggles and a lock-on reticle. Shadow: a
hooded figure and a cloak. Nightmare: horns and fangs. Spectre: a wraith and vapour. Reaper (the
capstone, one badge): a skull and a scythe.

- `ranks/r1.js` .. `ranks/r9.js` - one module per rank: its palette, its glyph (a 64x64 symbol),
  and its division evolution (`ornaments(ctx)`, d1..d3; Reaper is `ctx.div === 0`). The header of
  each module explains its design; the module API is documented at the top of `compose.js`.
- `compose.js` - builds the sprite: each rank's plate (machining taken from `plates.svgfrag`, the
  2.8.6 art), its ornaments, the depth lighting group and the glyph with its shadow and emboss.
- `build.js` - `node tools/rank-art/build.js` writes the sprite into both hub files. Run it after
  any change, then `tests/test_screen_rankbadge.py`.
- `lab.js` - renders ranks in headless Edge (no installs) at hero, ladder-chip and leaderboard
  sizes, on the dark panel and on the red hero:
  `node tools/rank-art/lab.js --ranks 6 --out r6.png --port 9406 --shipped`, or `--ladder` for all
  25 side by side. Always check the 20 px row: that is the leaderboard.

Rules the ladder keeps: plates darken up the ladder, ink stays at L >= .70, only Reaper uses the
accent red (in a glyph, through `var(--rk-accent, #c8102e)`), each rank's evolution is its own
(Nightmare owns horns and fangs, Reaper the scythe), and division 3 is always the biggest silhouette
in its rank. The lighting is gated by `var(--rk-depth, 0)`, which `depth.css` sets.
