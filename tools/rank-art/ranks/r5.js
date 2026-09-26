// Operator - selected rather than promoted: special operations, night raids.
// Glyph: quad-tube night-vision goggles (GPNVG-18), front view: a wide, low housing bar carrying a
// row of four black objective lenses - the inner pair big, the outer pair smaller and set a little
// lower where they splay out - with a low helmet-mount shoe on top.
// Palette: NVG-phosphor teal (a night-vision screen), well away from Soldier's green and Shadow's violet.
// Plate: faint phosphor scanlines across the face; the centre line is a reticle's stadia with mil ticks.
// Evolution (optics / HUD, a target being acquired then locked):
//   d2 HUD brackets "[ ]" stand off the plate's flanks - the target acquired (wider, same height);
//   d3 the lock-on box: four corner brackets enclose the whole badge, with cardinal crosshair ticks
//      above, below and to each side (the widest and tallest object, and square - no other rank is).
// Every piece is a machined bar (plate fill, ink edges) with a lit core line.

const f = v => +v.toFixed(2);

// x extent of the plate face at height y (face 40,18 64,30.1 64,54.1 40,74 16,54.1 16,30.1)
function faceHalf(y) {
  if (y < 30.1) return (y - 18) / 12.1 * 24;
  if (y <= 54.1) return 24;
  return (74 - y) / 19.9 * 24;
}
function scanlines(ink) {
  let d = "";
  for (let y = 21; y < 72; y += 2.6) {
    const h = faceHalf(y) - 1.2;
    if (h < 2) continue;
    d += `M${f(40 - h)},${y.toFixed(1)} H${f(40 + h)} `;
  }
  return `<path d="${d.trim()}" fill="none" stroke="${ink}" stroke-width=".5" opacity=".1"></path>`;
}

// A HUD corner bracket (left side; ctx.both mirrors it): outer corner (x, y), arms a (across) and
// b (down, or up when dy = -1), bar thickness t. Drawn as a machined bar with a lit core line.
function bracket(ctx, x, y, a, b, t, dy) {
  const { P, I } = ctx, h = t / 2;
  const bar = `M${x},${y} H${f(x + a)} V${f(y + dy * t)} H${f(x + t)} V${f(y + dy * b)} H${x} Z`;
  const core = `M${f(x + a - 1.4)},${f(y + dy * h)} H${f(x + h)} V${f(y + dy * (b - 1.4))}`;
  return ctx.both(bar, `fill="${P}" stroke="${I}" stroke-width="1.3" stroke-linejoin="round"`)
    + ctx.both(core, `fill="none" stroke="${I}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity=".85"`);
}
// A side bracket "[" (left side; ctx.both mirrors it): spine at x, from y1 to y2, arms a, thickness t.
function sideBracket(ctx, x, y1, y2, a, t) {
  const { P, I } = ctx, h = t / 2;
  const bar = `M${x},${y1} H${f(x + a)} V${f(y1 + t)} H${f(x + t)} V${f(y2 - t)} H${f(x + a)} V${y2} H${x} Z`;
  const core = `M${f(x + a - 1.4)},${f(y1 + h)} H${f(x + h)} V${f(y2 - h)} H${f(x + a - 1.4)}`;
  return ctx.both(bar, `fill="${P}" stroke="${I}" stroke-width="1.3" stroke-linejoin="round"`)
    + ctx.both(core, `fill="none" stroke="${I}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity=".85"`);
}
// A straight tick bar from (x1, y1) to (x2, y2) (axis-aligned), thickness t, with its core line.
function tick(ctx, x1, y1, x2, y2, t) {
  const { P, I } = ctx, h = t / 2, v = x1 === x2;
  const bar = v ? `M${f(x1 - h)},${y1} H${f(x1 + h)} V${y2} H${f(x1 - h)} Z` : `M${x1},${f(y1 - h)} H${x2} V${f(y1 + h)} H${x1} Z`;
  const core = v ? `M${x1},${f(y1 + 1.2)} V${f(y2 - 1.2)}` : `M${f(x1 + 1.2)},${y1} H${f(x2 - 1.2)}`;
  return `<path d="${bar}" fill="${P}" stroke="${I}" stroke-width="1.3" stroke-linejoin="round"></path>`
    + `<path d="${core}" fill="none" stroke="${I}" stroke-width="1.5" stroke-linecap="round" opacity=".85"></path>`;
}

// ---- the glyph (64 x 64 frame)
// one objective tube seen end-on: an ink rim (shaded darker below), the black lens, a bevel ring
// at the lens edge, a faint glass iris and a one-unit fleck of light. `splay` (the outer pair) turns
// the tube outward: its lens slides toward the outer rim and narrows a little.
function lens(cx, cy, R, r, splay = 0) {
  const lx = f(cx + splay), k = splay ? .9 : 1;
  const ell = (rr, attrs) => `<ellipse cx="${lx}" cy="${cy}" rx="${f(rr * k)}" ry="${f(rr)}" ${attrs}></ellipse>`;
  return `<circle cx="${f(cx)}" cy="${cy}" r="${R}" fill="currentColor"></circle>`
    + `<circle cx="${f(cx)}" cy="${cy}" r="${R}" fill="url(#r5-rim)"></circle>`
    + ell(r, `fill="#000" opacity=".92"`)
    + ell(r + .8, `fill="none" stroke="#000" stroke-width=".7" opacity=".3"`)
    + ell(r * .52, `fill="none" stroke="currentColor" stroke-width=".6" opacity=".16"`)
    + `<circle cx="${f(lx - r * k * .42)}" cy="${f(cy - r * .42)}" r="1" fill="currentColor" opacity=".55"></circle>`;
}
const IX = 9.7, IY = 38, IR = 9.4, Ir = 7.6;     // inner pair: x = 32 +- IX
const OX = 25.3, OY = 39.8, OR = 6.6, Or = 5.4;   // outer pair: x = 32 +- OX
// the housing: one low, flat bar the four tubes hang from
const HOUSING = "M1.4,40.2 V28.8 C1.4,26.8 2.6,25.4 4.6,25.4 H59.4 C61.4,25.4 62.6,26.8 62.6,28.8 V40.2 C62.6,42 61.4,43.2 59.6,43.2 H4.4 C2.6,43.2 1.4,42 1.4,40.2 Z";
// the helmet-mount shoe: a low dovetail on the housing's top centre, too small to change the silhouette
const MOUNT = "M26.8,25.8 L28.2,22 H35.8 L37.2,25.8 Z";

module.exports = {
  rank: 5,
  name: "Operator",
  plate: "oklch(.235 .035 190)",
  ink: "oklch(.77 .05 185)",
  defs: `<linearGradient id="r5-top" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#000" stop-opacity="0"></stop><stop offset=".5" stop-color="#000" stop-opacity=".1"></stop><stop offset="1" stop-color="#000" stop-opacity=".36"></stop></linearGradient>`
    + `<linearGradient id="r5-rim" x1=".2" y1="0" x2=".8" y2="1"><stop offset="0" stop-color="#000" stop-opacity="0"></stop><stop offset=".45" stop-color="#000" stop-opacity=".06"></stop><stop offset="1" stop-color="#000" stop-opacity=".42"></stop></linearGradient>`,
  glyph: `<symbol id="gl-5n" viewBox="0 0 64 64">`
    + [MOUNT, HOUSING].map(d => `<path d="${d}" fill="currentColor"></path><path d="${d}" fill="url(#r5-top)"></path>`).join("")
    + `<path d="M28.6,25.4 H35.4" fill="none" stroke="#000" stroke-width=".8" opacity=".4"></path>`
    // outer tubes behind the inner pair
    + lens(32 - OX, OY, OR, Or, -.7) + lens(32 + OX, OY, OR, Or, .7)
    + lens(32 - IX, IY, IR, Ir) + lens(32 + IX, IY, IR, Ir)
    + `</symbol>`,
  glyphBox: [16, 18.5, 48, 48],
  plateMod(plate, ctx) {
    const I = ctx.I;
    // the corner rivets would crowd the wide goggles: they go, and the plate's centre line becomes a
    // reticle's stadia line instead, with mil ticks below the glyph
    for (const x of [24, 56]) plate = plate.replace(`<circle cx="${x}" cy="36" r="1.8" fill="${I}" opacity=".6"></circle>`, "");
    const mils = `<path d="M37.4,62.6 H42.6 M38,66 H42 M38.6,69.4 H41.4" fill="none" stroke="${I}" stroke-width=".9" opacity=".55"></path>`;
    const face = plate.indexOf(`<polygon points="40,18 64,30.1 64,54.1 40,74 16,54.1 16,30.1" fill="none"`);
    return plate.slice(0, face) + scanlines(I) + mils + plate.slice(face);
  },
  ornaments(ctx) {
    const { div } = ctx;
    // d2: the target acquired - "[ ]" brackets off the plate's flat flanks (x 12 / 68, y 28.1-56.2)
    if (div === 2) return { behind: sideBracket(ctx, 1.8, 22.6, 61.8, 8.2, 4.4) };
    // d3: locked - a square lock-on box round the whole badge, with cardinal crosshair ticks
    if (div === 3) return {
      behind: bracket(ctx, 1.4, 1.4, 16, 16, 4.4, 1) + bracket(ctx, 1.4, 90.6, 16, 16, 4.4, -1)
        + tick(ctx, 40, 1.4, 40, 11.4, 4.4) + tick(ctx, 40, 80.6, 40, 90.6, 4.4)
        + tick(ctx, 1.4, 46, 9.6, 46, 4.4) + tick(ctx, 70.4, 46, 78.6, 46, 4.4),
    };
    return {};
  },
};
