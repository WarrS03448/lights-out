// Shadow - what is left of a person who keeps getting selected. The GLYPH, the half-shadowed
// plate and the cast shadow are APPROVED by Sam 2026-09-25 (keep them).
// Evolution: CLOAK AND DAGGER. d1 the bare plate; d2 a cloak hangs from the plate's shoulders in
// two swept, ragged flaps; d3 the cloak is whole - a hood peaks over the plate, the flaps run long
// and close under it in a wide ragged hem (wide, never a point: a point below is Nightmare's fang),
// and a dagger lies slantwise across the lower right, its point slipping out past the hem. The
// right side of the cloak falls in shadow, like the right half of the plate.
const BODY = "M33.5,2 C39.5,6.2 45.8,11.6 47,20 C48,27.4 45.6,32.6 42,36.6 C48.6,38.6 54.8,42.2 57.7,47.6 C59.2,50.6 59.7,54 59.7,58 L4.3,58 C4.3,54 4.8,50.6 6.3,47.6 C9.2,42.2 15.4,38.6 22,36.6 C18.4,32.6 16,27.4 17,20 C18.2,11.2 24.6,4.6 33.5,2 Z";
const FACE = "M32.6,12.4 C38.4,14.2 41.4,19.4 41.2,24.8 C41,30.6 37.2,34.6 32,35.4 C26.8,34.6 22.9,30.6 22.8,24.8 C22.7,19.2 26.2,13.8 32.6,12.4 Z";

const f = v => +v.toFixed(2);
// A closed path symmetric about x = 40, from its right half: a start point [x, y] then segments
// ["C", x1, y1, x2, y2, x, y] / ["L", x, y]. The half should start and end on x = 40; the left
// half is its mirror, walked backwards, so the outline has no seam down the middle.
function sym(right) {
  const pts = [right[0]];
  let d = `M${f(right[0][0])},${f(right[0][1])}`;
  for (const s of right.slice(1)) {
    if (s[0] === "C") { d += ` C${f(s[1])},${f(s[2])} ${f(s[3])},${f(s[4])} ${f(s[5])},${f(s[6])}`; pts.push([s[5], s[6]]); }
    else { d += ` L${f(s[1])},${f(s[2])}`; pts.push([s[1], s[2]]); }
  }
  for (let i = right.length - 1; i >= 1; i--) {
    const s = right[i], p = pts[i - 1];
    if (s[0] === "C") d += ` C${f(80 - s[3])},${f(s[4])} ${f(80 - s[1])},${f(s[2])} ${f(80 - p[0])},${f(p[1])}`;
    else d += ` L${f(80 - p[0])},${f(p[1])}`;
  }
  return d + " Z";
}
// A path's right half only (for the shaded side), as an open-then-closed path.
function half(right) {
  let d = `M${f(right[0][0])},${f(right[0][1])}`;
  for (const s of right.slice(1)) d += s[0] === "C" ? ` C${s.slice(1).map(f).join(" ")}` : ` L${f(s[1])},${f(s[2])}`;
  return d + " Z";
}

// d2: a flap per side, hung from the plate's shoulder, swept out and down to a ragged hem.
const FLAP2 = "M62,27.4 C66.8,27.6 70,29.4 71.4,32.8 C73.8,38.8 75.4,48 79.2,59.6"
  + " C78,58.8 76.6,57.8 75.4,56.4 C75.8,59.2 75.6,62 74.6,65"
  + " C73.6,62.8 72.4,61 71,59.6 C70.6,61 69.8,62.2 68.6,63.2 L62,58 Z";
const FOLD2 = "M69.8,33.4 C71.4,40 73,48.6 75.4,56.4";
// d3: the whole cloak - hood peak, shoulders, long flaps, a ragged hem that wraps under the plate.
const CLOAK3 = [[40, 1.1],
  ["C", 45.6, 3.9, 56.4, 8.9, 64.6, 15.6],
  ["C", 71.4, 21.4, 76, 28, 77.4, 35.4],
  ["C", 79, 44, 77.8, 57, 79.3, 71.8],
  ["C", 78.4, 70.8, 77, 69.6, 75.8, 67.8],
  ["C", 76, 71, 75.4, 74, 74, 77],
  ["C", 72.8, 74.6, 71.6, 72.6, 70.4, 70.8],
  // the hem closes under the plate: fewer, uneven tatters - the longest off-centre, a notch at the middle
  ["C", 69.6, 74.2, 68, 77.8, 65.6, 80.6],
  ["C", 64.2, 79.2, 62.4, 78.2, 60.6, 77.8],
  ["C", 59.4, 81.8, 57, 86, 53.6, 89],
  ["C", 52.2, 86.6, 50.8, 84.4, 49.2, 82.8],
  ["C", 48, 84.4, 46.6, 85.6, 45, 86.2],
  ["C", 43.6, 84.6, 41.8, 83.4, 40, 82.8]];
const FOLD3 = "M70.2,26.6 C74.4,33.4 75.8,43 75.6,52.6 C75.5,59 75.4,63.6 75.6,68";
const FOLD3B = "M69.4,57.6 C70.2,61.6 70.5,66 70.4,70.8";
// the drape under the plate, running down into the long tatter
const FOLD3C = "M56.8,67.4 C57.4,73 56.6,79.4 54.6,85.4";
// the hood's lip: the opening's edge, standing just off the plate's top edges
const LIP = "M9.6,27.4 C18.6,21.6 30.6,13.6 40,10 C49.4,13.6 61.4,21.6 70.4,27.4";
const HOOD_IN = "M9.6,27.4 C18.6,21.6 30.6,13.6 40,10 C49.4,13.6 61.4,21.6 70.4,27.4 L68,28.1 L40,14 L12,28.1 Z";
// the dagger: laid slantwise across the lower right of the cloak, its point slipping out past the
// hem. Drawn in local coordinates (u across the blade, v along it from the pommel) and turned to
// lie THETA degrees off vertical, pointing down and in, so the path data stays absolute.
const HILT = [75.3, 56.2], THETA = 27;
function daggerParts() {
  const a = THETA * Math.PI / 180, dir = [-Math.sin(a), Math.cos(a)], acr = [Math.cos(a), Math.sin(a)];
  const at = (u, v) => [HILT[0] + u * acr[0] + v * dir[0], HILT[1] + u * acr[1] + v * dir[1]];
  const pt = (u, v) => at(u, v).map(f).join(",");
  const path = segs => segs.map(([c, ...uv]) => {
    let s = c;
    for (let i = 0; i < uv.length; i += 2) s += (i ? " " : "") + pt(uv[i], uv[i + 1]);
    return s;
  }).join(" ") + " Z";
  return {
    blade: path([["M", -2.4, 10], ["C", -2.4, 18, -1.5, 25, 0, 33], ["C", 1.5, 25, 2.4, 18, 2.4, 10]]),
    bevel: path([["M", 0, 10], ["L", 2.4, 10], ["C", 2.4, 18, 1.5, 25, 0, 33]]),
    grip: path([["M", -1.3, 1.6], ["L", 1.3, 1.6], ["L", 1.3, 8], ["L", -1.3, 8]]),
    guard: path([["M", -4.6, 7.9], ["L", 4.6, 7.9], ["C", 6.1, 7.9, 6.1, 10.5, 4.6, 10.5], ["L", -4.6, 10.5], ["C", -6.1, 10.5, -6.1, 7.9, -4.6, 7.9]]),
    pommel: at(0, 0),
  };
}

module.exports = {
  rank: 6,
  name: "Shadow",
  plate: "oklch(.22 .045 272)",
  ink: "oklch(.75 .055 276)",
  defs: `<linearGradient id="r6-half" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff"></stop><stop offset=".46" stop-color="#fff"></stop><stop offset=".74" stop-color="#3a3a3a"></stop><stop offset="1" stop-color="#262626"></stop></linearGradient>`
    + `<mask id="r6-mask" maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64"><rect x="0" y="0" width="64" height="64" fill="url(#r6-half)"></rect><rect x="0" y="45.2" width="64" height="1.7" fill="#000"></rect><rect x="0" y="49.6" width="64" height="2.4" fill="#000"></rect><rect x="0" y="54" width="64" height="3.4" fill="#000"></rect></mask>`,
  glyph: `<symbol id="gl-6n" viewBox="0 0 64 64"><path d="${BODY}" fill="currentColor" mask="url(#r6-mask)"></path><path d="${FACE}" fill="#000" opacity=".9"></path></symbol>`,
  // half the face in shadow
  plateMod: (plate) => plate.replace(`<polygon points="40,18 64,30.1 64,54.1 40,74" fill="#000" opacity=".10"></polygon>`,
                                     `<polygon points="40,18 64,30.1 64,54.1 40,74" fill="#000" opacity=".3"></polygon>`),
  // a long cast shadow behind the figure
  glyphExtra: () => `<use href="#gl-6n" x="24.2" y="24.6" width="40" height="40" style="color:#000;opacity:.34"></use>`,
  ornaments(ctx) {
    const { P, I, div } = ctx;
    const line = `fill="none" stroke="${I}" stroke-width="1.4" stroke-linejoin="round"`;
    const fold = `fill="none" stroke="${I}" stroke-width="1" stroke-linecap="round" opacity=".55"`;
    if (div === 2) {
      return {
        behind: ctx.both(FLAP2, `fill="${P}"`)
          + `<path d="${FLAP2}" fill="#000" opacity=".3"></path>`
          + ctx.both(FOLD2, fold)
          + ctx.both(FLAP2, line),
      };
    }
    if (div === 3) {
      const cloak = sym(CLOAK3), dg = daggerParts();
      const metal = `fill="${P}" stroke="${I}" stroke-width="1.2" stroke-linejoin="round"`;
      return {
        behind: `<path d="${cloak}" fill="${P}"></path>`
          + `<path d="${half(CLOAK3)}" fill="#000" opacity=".3"></path>`
          + `<path d="${HOOD_IN}" fill="#000" opacity=".45"></path>`
          + ctx.both(FOLD3, fold) + ctx.both(FOLD3B, fold) + ctx.both(FOLD3C, fold)
          + `<path d="${LIP}" fill="none" stroke="${I}" stroke-width="1" opacity=".7"></path>`
          + `<path d="${cloak}" ${line}></path>`
          + `<path d="${dg.blade}" fill="${I}" opacity=".9"></path>`
          + `<path d="${dg.bevel}" fill="#000" opacity=".38"></path>`
          + `<path d="${dg.grip}" ${metal}></path>`
          + `<path d="${dg.guard}" ${metal}></path>`
          + `<circle cx="${f(dg.pommel[0])}" cy="${f(dg.pommel[1])}" r="2" ${metal}></circle>`,
      };
    }
    return {};
  },
};
