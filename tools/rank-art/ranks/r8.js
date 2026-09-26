// Spectre - no longer a person at all.
// Glyph: a wraith adrift - no shoulders, no ground: a tall hood with a hollow for a face and two
// cold eyes, its shroud leaning and narrowing into a wisp that trails off to one side, ragged along
// the trailing edge, with a faint motion echo behind it.
// Plate: a broken dashed rim just off the plate (the plate's own afterimage).
// Evolution (ethereal vapour): every other rank's ornaments are solid metal; Spectre's are pale,
// see-through vapour. d2 wisps rise off the sides and dissolve; d3 taller wisps, a broken halo
// floating above, and a vapour tail trailing below.
// Ink .72 / plate .15 with the shroud fading from mid-height: the d1 face measures mean L* 27.5,
// at Nightmare's 27.6 and under Shadow's 28.0 (r8tools/lstar.js), so the ladder keeps darkening.
const INK = "oklch(.72 .02 228)";

// ---- geometry: a Catmull-Rom centreline swept into a tapered ribbon (absolute M/L only, so
// ctx.mirror() can flip it), a band of a ring seen from above, and a closed smooth outline
function spline(pts, n = 10) {
  const out = [];
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(i - 1, 0)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(i + 2, pts.length - 1)];
    for (let k = 0; k < n; k++) {
      const t = k / n, t2 = t * t, t3 = t2 * t;
      out.push([0, 1].map(j => 0.5 * (2 * p1[j] + (-p0[j] + p2[j]) * t + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2 + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3)));
    }
  }
  out.push(pts[pts.length - 1]);
  return out;
}
const f2 = v => +v.toFixed(2);
const pd = pts => "M" + pts.map(p => f2(p[0]) + "," + f2(p[1])).join(" L") + " Z";
function ribbon(pts, w0, w1) {
  const s = spline(pts), N = s.length, L = [0];
  for (let i = 1; i < N; i++) L.push(L[i - 1] + Math.hypot(s[i][0] - s[i - 1][0], s[i][1] - s[i - 1][1]));
  const a = [], b = [];
  for (let i = 0; i < N; i++) {
    const p = s[Math.max(i - 1, 0)], q = s[Math.min(i + 1, N - 1)];
    let dx = q[0] - p[0], dy = q[1] - p[1];
    const l = Math.hypot(dx, dy) || 1; dx /= l; dy /= l;
    const w = (w1 + (w0 - w1) * (1 - L[i] / L[N - 1])) / 2;
    a.push([s[i][0] - dy * w, s[i][1] + dx * w]);
    b.push([s[i][0] + dy * w, s[i][1] - dx * w]);
  }
  return pd(a.concat(b.reverse()));
}
// ty: the band's thickness at the top and bottom of the ellipse (t at the sides); jag: the two
// ends are snapped, not cut - a tooth on one end, the matching notch on the other
function ringBand(cx, cy, rx, ry, t, a0, a1, n = 36, ty = t * 0.5, jag = 0) {
  const o = [], i = [], at = (a, k) => [cx + (rx - t * k) * Math.cos(a * Math.PI / 180), cy + (ry - ty * k) * Math.sin(a * Math.PI / 180)];
  for (let k = 0; k <= n; k++) {
    const a = a0 + (a1 - a0) * k / n;
    o.push(at(a, 0)); i.push(at(a, 1));
  }
  const tooth = jag ? [at(a1 + jag, 0.5)] : [], notch = jag ? [at(a0 + jag, 0.5)] : [];
  return pd(o.concat(tooth, i.reverse(), notch));
}
// A closed outline through points [x, y, sharp?]: smooth (Catmull-Rom as cubic C) except at the
// points flagged sharp, which become corners - the ragged tatters.
function outline(P) {
  const N = P.length, tan = i => {
    if (P[i][2]) return [0, 0];
    const a = P[(i - 1 + N) % N], b = P[(i + 1) % N];
    return [(b[0] - a[0]) / 6, (b[1] - a[1]) / 6];
  };
  let d = `M${f2(P[0][0])},${f2(P[0][1])}`;
  for (let i = 0; i < N; i++) {
    const p = P[i], q = P[(i + 1) % N], m = tan(i), n = tan((i + 1) % N);
    d += ` C${f2(p[0] + m[0])},${f2(p[1] + m[1])} ${f2(q[0] - n[0])},${f2(q[1] - n[1])} ${f2(q[0])},${f2(q[1])}`;
  }
  return d + " Z";
}

// ---- the glyph (64 x 64): hood crown top right of centre, the shroud leaning and curling off to
// the lower left, the trailing (left) edge torn into tatters
// shift absolute M/C/L path data (x,y pairs only)
const shift = (d, dx, dy = 0) => d.replace(/(-?\d*\.?\d+),(-?\d*\.?\d+)/g, (_, x, y) => f2(+x + dx) + "," + f2(+y + dy));
const HX = -2.4;   // the hood sits a little right of centre; the streamers balance it on the left
const BODY = outline([
  [33.6, 1.4], [41, 3.8], [45, 10.6], [45.8, 18.6], [46.4, 27], [47.2, 35.4], [45, 43.6],
  [40, 50.6], [32.4, 56.2], [22.4, 60.4], [4.2, 62.8, 1],
  [13.6, 56.8, 1], [5.6, 52.4, 1], [15.8, 49.4, 1], [9, 42.6, 1], [18.2, 41.8, 1],
  [19.6, 34.6], [20.2, 26.4], [21, 17.6], [24.8, 7.6],
]);
const HOLLOW = shift("M36.2,6.6 C40.8,9 43.4,13.6 43.2,19.4 C43,25.4 40,30.2 36,33 C32,30.2 29,25.4 28.8,19.4 C28.6,13.6 31.6,9 36.2,6.6 Z", HX);
const HOODEDGE = shift("M26.4,25.2 C31,33.4 41.4,33.4 46,25.4 C43.6,33.2 38.8,36.8 36,37 C33.2,36.8 28.6,33.2 26.4,25.2 Z", HX);
const FOLDS = ribbon([[37, 36.4], [36.4, 44], [31, 51.4], [20.6, 57.6]], 2.6, 0.3)
  + " " + ribbon([[43.4, 33.4], [42.6, 41], [37.8, 48.6]], 2, 0.3)
  + " " + ribbon([[27.6, 36.6], [25, 42.4], [19, 47.6]], 2, 0.3);
// two small cold glints, slanted in, not almond "alien" eyes
const EYES = "M29.2,18.4 C30.6,18.3 32,18.8 32.7,20 C31.2,20.6 29.8,20 29.2,18.4 Z M38.4,18.4 C37,18.3 35.6,18.8 34.9,20 C36.4,20.6 37.8,20 38.4,18.4 Z";

// ---- the evolution: left-side wisps (mirrored), the tail, the halo
const WISPS2 = [
  ribbon([[14, 54], [8.6, 51.8], [4.8, 47], [5.6, 41], [4, 35.4], [2.8, 31]], 6.4, 1),
  ribbon([[13, 46], [8, 44.6], [4, 40.4], [3.8, 34.8], [6.4, 30.4], [9.4, 28]], 5, 0.8),
  ribbon([[13, 38], [9.6, 36.2], [8.6, 32], [10.6, 28.4]], 3.2, 0.6),
];
const WISPS3 = [
  ribbon([[14, 58], [7.4, 55.4], [3.4, 49.4], [5.4, 42.4], [3.4, 35.6], [0.8, 29], [2.4, 22.4]], 7, 1),
  ribbon([[13, 48], [8, 45.2], [2.8, 40], [2.4, 32.8], [6.2, 27], [9.2, 21], [7.4, 14.6]], 5.6, 0.8),
  ribbon([[13, 38], [9.8, 34.2], [9.6, 28.8], [12.2, 23.6], [11.6, 18.4]], 3.8, 0.6),
];
const HAZE2 = [9.4, 45, 6, 11], HAZE3 = [8.6, 43, 7, 14.5];
const TAIL = [ribbon([[40, 71], [41.2, 78.4], [38.6, 83.8], [38.6, 88.4], [40.8, 91.4]], 11, 1),
              ribbon([[40, 73], [37.6, 79.4], [40.8, 84.4], [42, 88.8]], 4.2, 0.6)];

module.exports = {
  rank: 8,
  name: "Spectre",
  plate: "oklch(.15 .026 245)",
  ink: INK,
  defs: `<linearGradient id="r8-fade" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff"></stop><stop offset=".5" stop-color="#fff"></stop><stop offset="1" stop-color="#555"></stop></linearGradient>`
    + `<mask id="r8-mask" maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64"><rect x="0" y="0" width="64" height="64" fill="url(#r8-fade)"></rect></mask>`
    // the side wisps thin out as they rise
    + `<linearGradient id="r8-rise" gradientUnits="userSpaceOnUse" x1="0" y1="8" x2="0" y2="46"><stop offset="0" stop-color="#3a3a3a"></stop><stop offset=".55" stop-color="#d8d8d8"></stop><stop offset="1" stop-color="#fff"></stop></linearGradient>`
    + `<mask id="r8-vap" maskUnits="userSpaceOnUse" x="-10" y="-10" width="100" height="112"><rect x="-10" y="-10" width="100" height="112" fill="url(#r8-rise)"></rect></mask>`
    // the tail thins out as it falls
    + `<linearGradient id="r8-tail" gradientUnits="userSpaceOnUse" x1="0" y1="74" x2="0" y2="92"><stop offset="0" stop-color="${INK}"></stop><stop offset="1" stop-color="${INK}" stop-opacity=".45"></stop></linearGradient>`,
  glyph: `<symbol id="gl-8n" viewBox="0 0 64 64">`
    + `<path d="${BODY}" transform="translate(-4 3)" fill="currentColor" opacity=".14" mask="url(#r8-mask)"></path>`
    + `<path d="${BODY}" fill="currentColor" mask="url(#r8-mask)"></path>`
    + `<path d="${FOLDS}" fill="#000" opacity=".42"></path>`
    + `<path d="${HOODEDGE}" fill="#000" opacity=".4"></path>`
    + `<path d="${HOLLOW}" fill="#000" opacity=".9"></path>`
    + `<circle cx="31" cy="19.4" r="3.2" fill="currentColor" opacity=".14"></circle>`
    + `<circle cx="36.6" cy="19.4" r="3.2" fill="currentColor" opacity=".14"></circle>`
    + `<path d="${EYES}" fill="currentColor"></path>`
    + `</symbol>`,
  plateMod(plate, ctx) {
    // the afterimage: a dashed rim ~1.5 units off the plate. The dashes tile the perimeter exactly
    // and a gap is centred on the top vertex, so nothing runs up into d3's halo.
    const pts = [[40, 12.4], [69.6, 27.2], [69.6, 56.9], [40, 79.9], [10.4, 56.9], [10.4, 27.2]];
    const per = pts.reduce((a, p, i) => a + Math.hypot(pts[(i + 1) % 6][0] - p[0], pts[(i + 1) % 6][1] - p[1]), 0);
    const unit = per / 24, dash = unit * 0.61, gap = unit - dash;
    const ring = `<polygon points="${pts.map(p => p.join(",")).join(" ")}" fill="none" stroke="${ctx.I}" stroke-width=".9" stroke-dasharray="${f2(dash)} ${f2(gap)}" stroke-dashoffset="${f2(dash + gap / 2)}" opacity=".25"></polygon>`;
    return ring + plate;
  },
  ornaments(ctx) {
    const { I, div, mirror } = ctx;
    // each strand is its own translucent layer, so where two cross the vapour thickens
    // a pale haze behind the strands (two steps, so its edge is soft), then the strands
    const haze = (x, y, rx, ry) => [1, 0.66].map(k => `<ellipse cx="${x}" cy="${y}" rx="${f2(rx * k)}" ry="${f2(ry * k)}" fill="${I}" opacity=".17"></ellipse>`).join("");
    const wisps = (ds, [hx, hy, hrx, hry]) => `<g mask="url(#r8-vap)">` + haze(hx, hy, hrx, hry) + haze(80 - hx, hy, hrx, hry)
      + ds.concat(ds.map(mirror)).map(d => `<path d="${d}" fill="${I}" opacity=".6"></path>`).join("") + `</g>`;
    if (div === 2) return { behind: wisps(WISPS2, HAZE2) };
    if (div === 3) {
      // one clear break (26 degrees) in the far right of the ring, its ends snapped
      const halo = ringBand(40, 6.8, 13.6, 5.8, 4.6, -52, 282, 36, 3, 6);
      return {
        behind: wisps(WISPS3, HAZE3) + haze(40, 84.4, 4.2, 7.4)
          + TAIL.map(d => `<path d="${d}" fill="url(#r8-tail)" opacity=".95"></path>`).join("")
          + `<g transform="rotate(-8 40 6.8)"><path d="${halo}" fill="${I}" opacity=".92"></path></g>`,
      };
    }
    return {};
  },
};
