// Private - the lowest enlisted rank, one stripe on the sleeve. Olive drab plate, khaki ink.
// Glyph: a single bold chevron, cut like a metal insignia (a ridge down each arm, lit from the top left).
// Plate: the centre-line machining is dropped (under a chevron it read as an arrow shaft).
// Evolution: RANK STRIPES. d1 the bare plate, two stripe ends just showing in each flank bevel;
// d2 two chevron stripes behind the plate, their arms stepping out of both flanks; d3 three longer
// stripes, a chevron crest over the point and a rocker slung beneath - a full sergeant stack.
// Every stripe is bevelled like the glyph: its upper half catches the light (ink wash), its lower
// half turns away (shade) - which also keeps the stripes readable as bands at 20 px.
const f = v => +v.toFixed(2);
const pts = a => a.map(p => f(p[0]) + "," + f(p[1])).join(" ");
const RAD = Math.PI / 180;

// ---- glyph: one chevron in the 64x64 box
const TAN = Math.tan(40 * RAD), COS = Math.cos(40 * RAD);
const AY = 10.5, T = 14, W = 27;                  // outer apex y, arm thickness, half span
const V = T / COS;                                // vertical thickness of an arm
const yo = dx => AY + TAN * dx, yi = dx => AY + V + TAN * dx, ym = dx => AY + V / 2 + TAN * dx;
const CHEV = [[32 - W, yo(W)], [32, AY], [32 + W, yo(W)], [32 + W, yi(W)], [32, AY + V], [32 - W, yi(W)]];
// facets: light from the top left - left upper face brightest, left lower face darkest
const R_UP = [[32, AY], [32 + W, yo(W)], [32 + W, ym(W)], [32, ym(0)]];
const L_LO = [[32 - W, ym(W)], [32, ym(0)], [32, AY + V], [32 - W, yi(W)]];
const R_LO = [[32, ym(0)], [32 + W, ym(W)], [32 + W, yi(W)], [32, AY + V]];

// ---- ornaments (drawn on the right; ctx.both mirrors them)
const ST = 4.8, SV = ST / COS;                    // a stripe's thickness, and its vertical thickness
const sy = (y0, x) => y0 + TAN * (x - 40);        // top edge of a stripe whose outer apex is (40, y0)
// the right arm of a chevron stripe, from inside the plate (x0) out to x, end cut vertical
function arm(y0, x, x0 = 66.8) {
  return `M${x0},${f(sy(y0, x0))} L${f(x)},${f(sy(y0, x))} L${f(x)},${f(sy(y0, x) + SV)} L${x0},${f(sy(y0, x0) + SV)} Z`;
}
function armShade(y0, x, x0 = 66.8) {           // its lower half, turned away from the light
  return `M${x0},${f(sy(y0, x0) + SV / 2)} L${f(x)},${f(sy(y0, x) + SV / 2)} L${f(x)},${f(sy(y0, x) + SV)} L${x0},${f(sy(y0, x0) + SV)} Z`;
}
function armLit(y0, x, x0 = 66.8) {             // its upper half, catching the light
  return `M${x0},${f(sy(y0, x0))} L${f(x)},${f(sy(y0, x))} L${f(x)},${f(sy(y0, x) + SV / 2)} L${x0},${f(sy(y0, x0) + SV / 2)} Z`;
}
// a whole chevron stripe (outer apex (40, ay), half span w), ends cut vertical; from/to take a
// slice of its thickness (0..1) - the lit upper half, the shaded lower half
function chevron(ay, w, from = 0, to = 1) {
  const t = ay + from * SV, b = ay + to * SV;
  return [[40 - w, t + TAN * w], [40, t], [40 + w, t + TAN * w], [40 + w, b + TAN * w], [40, b], [40 - w, b + TAN * w]];
}
// the rocker: a stripe slung from under the lowest arms' ends (x = 80 - xe .. xe) down beneath the
// point; its top edge at the ends is yEnd, at the middle yMid. Ends cut vertical like the arms.
function rocker(xe, yEnd, yMid) {
  const q = (e, m) => 2 * m - e;                  // quadratic control y for end e, midpoint m
  const edge = (e, m, rev) => rev ? `${f(80 - xe)},${f(e)} Q40,${f(q(e, m))} ${f(xe)},${f(e)}` : `${f(xe)},${f(e)} Q40,${f(q(e, m))} ${f(80 - xe)},${f(e)}`;
  const band = (from, to) => `M${edge(yEnd + from * SV, yMid + from * ST)} L${edge(yEnd + to * SV, yMid + to * ST, true)} Z`;
  return { band: band(0, 1), lit: band(0, 0.5), shade: band(0.5, 1) };
}

module.exports = {
  rank: 2,
  name: "Private",
  plate: "oklch(.31 .048 118)",
  ink: "oklch(.85 .062 110)",
  glyphBox: [19, 24, 42, 42],
  glyph: `<symbol id="gl-2n" viewBox="0 0 64 64">`
    + `<polygon points="${pts(CHEV)}" fill="currentColor"></polygon>`
    + `<polygon points="${pts(R_UP)}" fill="#000" opacity=".12"></polygon>`
    + `<polygon points="${pts(R_LO)}" fill="#000" opacity=".2"></polygon>`
    + `<polygon points="${pts(L_LO)}" fill="#000" opacity=".26"></polygon>`
    + `</symbol>`,
  plateMod(plate) {
    // the rank's machining was a centre line - under a chevron it reads as an arrow shaft; drop it
    return plate.replace(/<line x1="40" y1="18" x2="40" y2="74"[^>]*><\/line>/, "");
  },
  ornaments(ctx) {
    const { P, I, div } = ctx;
    const a = `fill="${P}" stroke="${I}" stroke-width="1.35" stroke-linejoin="round"`;
    const sh = `fill="#000" opacity=".28"`;
    const lit = `fill="${I}" opacity=".34"`;
    const arms = (list, x) => list.map(y0 => ctx.both(arm(y0, x), a) + ctx.both(armLit(y0, x), lit) + ctx.both(armShade(y0, x), sh)).join("");
    if (div === 1) {
      // the hint: the two stripes' ends show in the flank bevels, ready to step out
      const tick = y0 => ctx.both(`M64.6,${f(sy(y0, 64.6) + 1)} L67.4,${f(sy(y0, 67.4) + 1)} L67.4,${f(sy(y0, 67.4) + SV - 1)} L64.6,${f(sy(y0, 64.6) + SV - 1)} Z`, `fill="${I}" opacity=".5"`);
      return { front: tick(12.4) + tick(21.8) };
    }
    if (div === 2) return { behind: arms([12.4, 21.8], 77.4) };
    if (div === 3) {
      const r = rocker(79.2, sy(25.8, 79.2) + SV + 3.1, 84.4);
      return {
        behind: arms([7, 16.4, 25.8], 79.2)
          + `<path d="${r.band}" ${a}></path><path d="${r.lit}" ${lit}></path><path d="${r.shade}" ${sh}></path>`
          + `<polygon points="${pts(chevron(0.9, 16.2))}" ${a}></polygon><polygon points="${pts(chevron(0.9, 16.2, 0, .5))}" ${lit}></polygon><polygon points="${pts(chevron(0.9, 16.2, .5, 1))}" ${sh}></polygon>`,
      };
    }
    return {};
  },
};
