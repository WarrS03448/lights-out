// Reaper - rank 9, the capstone: the end of it. One badge (rk-25), no divisions (ctx.div === 0).
// Glyph: a cracked skull set on the red event-horizon ring (a black hole with a red rim), red
// glints in the eye sockets. The only badge that carries the accent red. The lower skull is kept
// in shadow so the hole and the ring, not the bone, carry the badge.
// Evolution (own theme: death / the scythe): ONE scythe, carried behind the plate. Its snath is a
// straight pole running diagonally behind the shield - out above-right and below-left - and the
// blade is fixed across its top end, sweeping over the head of the shield to hook down past the
// left shoulder, concave edge inward, with a red glint on the edge. The straight shaft and the
// asymmetry are what make it a scythe and not a pair of horns (horns are Nightmare's). Iron rings
// bind the blade to the snath and shoe its rounded foot. No crown: the blade carries the top, and a
// row of spikes there would bring back Nightmare's crest.

// symmetric closed path about x = cx from the right half: start on the axis, segments down the
// right side ([c1x,c1y,c2x,c2y,x,y] cubic or [x,y] line), the last one ending on the axis.
function sym(start, segs, cx = 32) {
  const m = x => +(2 * cx - x).toFixed(3);
  const f = v => +v.toFixed(3);
  let d = `M${f(start[0])},${f(start[1])}`;
  for (const s of segs) d += s.length === 6 ? ` C${f(s[0])},${f(s[1])} ${f(s[2])},${f(s[3])} ${f(s[4])},${f(s[5])}` : ` L${f(s[0])},${f(s[1])}`;
  const pts = [start, ...segs.map(s => s.slice(-2))];
  for (let i = segs.length - 1; i >= 0; i--) {
    const s = segs[i], p0 = pts[i];
    d += s.length === 6 ? ` C${m(s[2])},${f(s[3])} ${m(s[0])},${f(s[1])} ${m(p0[0])},${f(p0[1])}` : ` L${m(p0[0])},${f(p0[1])}`;
  }
  return d + " Z";
}
// mirror absolute "x,y" pairs of glyph path data about x = 32
const mirrorG = d => d.replace(/(-?\d*\.?\d+),(-?\d*\.?\d+)/g, (_, x, y) => `${+(64 - x).toFixed(3)},${y}`);
const ACC = "var(--rk-accent,#c8102e)";

// ---------------------------------------------------------------- glyph (64 x 64)
const SKULL = sym([32, 6.5], [
  [42, 6.5, 50.5, 13, 50.5, 24.5],      // dome to the widest point
  [50.5, 30, 49.2, 33.5, 48.2, 35.5],   // temple
  [49, 37.5, 48.6, 40.2, 46.4, 42],     // cheekbone
  [45, 43.1, 43.4, 43.6, 42.6, 45.2],   // under the cheek
  [42.4, 50],                           // upper jaw
  [42.2, 54.8, 38, 57.5, 32, 57.5],     // chin
]);
const EYE_R = "M34.6,30.4 L45.4,26.2 C47.8,27.8 48,32.8 45.2,35.6 C42.6,38 37.6,37.8 35.6,35 C34.6,33.6 34.3,31.8 34.6,30.4 Z";
const NOSE = "M32,37.6 C30.4,40.2 29,42.8 29.8,44 C30.4,44.8 31.5,44.3 32,43.4 C32.5,44.3 33.6,44.8 34.2,44 C35,42.8 33.6,40.2 32,37.6 Z";
const CHEEK_R = "M46.4,42 C45,43.1 43.4,43.6 42.6,45.2 L42.5,47.8 C41.1,46 41.2,43.2 43.4,41.6 C44.4,40.9 45.6,41.3 46.4,42 Z";
// the mouth: one dark band (no row of teeth - teeth are Nightmare's)
const MOUTH = sym([32, 46.3], [[37.9, 46.3], [38.3, 46.5, 38.4, 46.9, 38.4, 47.4], [38.4, 51.2], [36.6, 53.3, 34.4, 53.7, 32, 53.7]]);
const RC = [32, 31], RR = 25.4;   // the event-horizon ring

const glyph = `<symbol id="gl-9n" viewBox="0 0 64 64">`
  // the black hole and its red rim (cut away round the skull)
  + `<circle cx="${RC[0]}" cy="${RC[1]}" r="${RR}" fill="#000" opacity=".55"></circle>`
  + `<circle cx="${RC[0]}" cy="${RC[1]}" r="23" fill="none" stroke-width="1.8" style="stroke:${ACC};opacity:.16" mask="url(#r9-cut)"></circle>`
  + `<circle cx="${RC[0]}" cy="${RC[1]}" r="${RR}" fill="none" stroke-width="3.6" style="stroke:${ACC}" mask="url(#r9-cut)"></circle>`
  // the skull, its lower half sunk in shadow
  + `<path d="${SKULL}" fill="currentColor"></path>`
  + `<path d="${SKULL}" fill="url(#r9-jaw)"></path>`
  + `<path d="${CHEEK_R} ${mirrorG(CHEEK_R)}" fill="#000" opacity=".45"></path>`
  + `<path d="${EYE_R} ${mirrorG(EYE_R)}" fill="#000" opacity=".92"></path>`
  + `<path d="${NOSE}" fill="#000" opacity=".85"></path>`
  + `<path d="${MOUTH}" fill="#000" opacity=".66"></path>`
  // an old crack across the crown of the skull
  + `<path d="M41.6,8.4 L39.4,12.6 L41.4,15 L38.2,19.6 L39.2,20.4 L42.8,15.2 L40.8,12.8 L42.8,8.9 Z" fill="#000" opacity=".5"></path>`
  // red glints in the sockets
  + `<circle cx="39.4" cy="31.6" r="1.7" style="fill:${ACC}"></circle><circle cx="24.6" cy="31.6" r="1.7" style="fill:${ACC}"></circle>`
  + `</symbol>`;

const defs = `<mask id="r9-cut" maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">`
  + `<rect x="0" y="0" width="64" height="64" fill="#fff"></rect>`
  + `<path d="${SKULL}" fill="#000" stroke="#000" stroke-width="4.4" stroke-linejoin="round"></path></mask>`
  + `<linearGradient id="r9-jaw" gradientUnits="userSpaceOnUse" x1="0" y1="27" x2="0" y2="57.5">`
  + `<stop offset="0" stop-color="#000" stop-opacity="0"></stop><stop offset=".4" stop-color="#000" stop-opacity=".18"></stop><stop offset=".65" stop-color="#000" stop-opacity=".32"></stop><stop offset="1" stop-color="#000" stop-opacity=".46"></stop></linearGradient>`;

// ---------------------------------------------------------------- ornaments (badge space 80 x 92)
const f2 = v => +v.toFixed(2);
const pt = p => `${f2(p[0])},${f2(p[1])}`;
function bez(P, t) {
  const u = 1 - t;
  return [0, 1].map(k => u * u * u * P[0][k] + 3 * u * u * t * P[1][k] + 3 * u * t * t * P[2][k] + t * t * t * P[3][k]);
}
function smooth(pts) {
  // Catmull-Rom through the points -> absolute cubic path data (no M)
  let d = "";
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)];
    d += ` C${f2(p1[0] + (p2[0] - p0[0]) / 6)},${f2(p1[1] + (p2[1] - p0[1]) / 6)} ${f2(p2[0] - (p3[0] - p1[0]) / 6)},${f2(p2[1] - (p3[1] - p1[1]) / 6)} ${f2(p2[0])},${f2(p2[1])}`;
  }
  return d;
}
// A strip along a cubic C between two offsets (numbers, or functions of t) measured along its
// normal (the left-hand normal of the direction of travel), sampled from t0 to t1.
function strip(C, off0, off1, t0 = 0, t1 = 1, N = 16) {
  const side = off => {
    const pts = [];
    for (let i = 0; i <= N; i++) {
      const t = t0 + (t1 - t0) * i / N, [x, y] = bez(C, t);
      const [xa, ya] = bez(C, Math.min(1, t + 1e-3)), [xb, yb] = bez(C, Math.max(0, t - 1e-3));
      const L = Math.hypot(xa - xb, ya - yb), nx = (ya - yb) / L, ny = -(xa - xb) / L;
      const o = typeof off === "function" ? off(t) : off;
      pts.push([x + nx * o, y + ny * o]);
    }
    return pts;
  };
  const a = side(off1), b = side(off0).reverse();
  return `M${pt(a[0])}${smooth(a)} L${pt(b[0])}${smooth(b)} Z`;
}
const cub = C => ` C${pt(C[1])} ${pt(C[2])} ${pt(C[3])}`;

// the snath: a straight pole behind the plate, from above-right to below-left
const SN_TOP = [74.6, 1.6], SN_FOOT = [11.6, 87.2], SN_HW = 2.7;
const SN_LEN = Math.hypot(SN_FOOT[0] - SN_TOP[0], SN_FOOT[1] - SN_TOP[1]);
const SN_U = [(SN_FOOT[0] - SN_TOP[0]) / SN_LEN, (SN_FOOT[1] - SN_TOP[1]) / SN_LEN];
const SN_N = [-SN_U[1], SN_U[0]];   // across the pole (+ is its upper-left, lit side)
const along = (s, o = 0) => [SN_TOP[0] + SN_U[0] * s + SN_N[0] * o, SN_TOP[1] + SN_U[1] * s + SN_N[1] * o];
const band = (s0, s1, hw, o0 = -hw) => `M${pt(along(s0, o0))} L${pt(along(s1, o0))} L${pt(along(s1, hw))} L${pt(along(s0, hw))} Z`;
// the pole from s0 down to s1, its head rounded, its foot rounded too or (roundFoot false) cut square
const pole = (s0, s1, hw, roundFoot) => `M${pt(along(s1 - (roundFoot ? hw * .6 : 0), -hw))} L${pt(along(s0 + hw * .6, -hw))} C${pt(along(s0 - hw * .15, -hw))} ${pt(along(s0 - hw * .15, hw))} ${pt(along(s0 + hw * .6, hw))} L${pt(along(s1 - (roundFoot ? hw * .6 : 0), hw))}`
  + (roundFoot ? ` C${pt(along(s1 + hw * .15, hw))} ${pt(along(s1 + hw * .15, -hw))} ${pt(along(s1 - hw * .6, -hw))}` : "") + " Z";

// the blade, drawn from two curves: the BACK (heel -> point) arches over the shield and hooks down
// past the left shoulder; the EDGE (point -> heel) is the concave cutting side facing the shield.
// Both ends of the heel sit on the snath's axis, so the pole covers the joint.
const HEEL_B = along(2.4), HEEL_E = along(15.5), TIP = [6.2, 35];
const BACK = [HEEL_B, [54, -3], [1.5, 1.4], TIP];
const EDGE = [TIP, [5.6, 25], [28, 9.2], HEEL_E];
const BLADE = `M${pt(HEEL_B)}${cub(BACK)}${cub(EDGE)} Z`;
const GLINT = [.12, .58];   // along the EDGE, from the point

module.exports = {
  rank: 9,
  name: "Reaper",
  plate: "oklch(.12 .012 270)",
  ink: "oklch(.72 .012 270)",
  defs,
  glyph,
  glyphBox: [18, 22, 44, 44],
  plateMod(plate) {
    // the dots under the ring go (the hole does their work)
    return plate.replace(/<circle cx="(24|56)" cy="36" r="1\.8"[^>]*><\/circle>/g, "");
  },
  ornaments(ctx) {
    const { P, I } = ctx;
    const a = `fill="${P}" stroke="${I}" stroke-width="1.4" stroke-linejoin="round"`;
    const iron = `fill="${I}" stroke="${P}" stroke-width=".6" stroke-linejoin="round"`;
    const E = SN_LEN;   // the foot of the snath
    // the snath, its lit upper side, and the iron ring shoeing its foot
    const snath = `<path d="${pole(0, E, SN_HW, true)}" ${a}></path>`
      + `<path d="${band(2.4, E - 6.2, 1.5, .2)}" fill="#fff" opacity=".08"></path>`
      + `<path d="${band(E - 6.2, E - 3.2, SN_HW + .9)}" ${iron}></path>`
      + `<path d="${band(E - 6.2, E - 3.2, 0, -SN_HW - .9)}" fill="#000" opacity=".3"></path>`;
    // the blade: body, lit back, honed bevel along the edge, red glint on the edge
    const bevel = t => Math.min(1.8, 7 * t);
    const blade = `<path d="${BLADE}" ${a}></path>`
      + `<path d="${strip(BACK, 0, t => Math.min(1.5, 9 * (1 - t)), 0, .97)}" fill="#fff" opacity=".08"></path>`
      + `<path d="${strip(EDGE, 0, bevel, .015, 1)}" fill="${I}" opacity=".34"></path>`
      + `<path d="${strip(EDGE, 0, t => 1.1 * Math.sin(Math.PI * (t - GLINT[0]) / (GLINT[1] - GLINT[0])), GLINT[0], GLINT[1])}" style="fill:${ACC}"></path>`;
    // the head of the snath drawn again over the blade's heel (its square end hides under the
    // collar), and the iron collar binding the blade to it
    const head = `<path d="${pole(0, 17.2, SN_HW)}" ${a}></path>`
      + `<path d="${band(2.4, 17.2, 1.5, .2)}" fill="#fff" opacity=".08"></path>`
      + `<path d="${band(15.8, 18.6, SN_HW + .9)}" ${iron}></path>`
      + `<path d="${band(15.8, 18.6, 0, -SN_HW - .9)}" fill="#000" opacity=".3"></path>`;
    return { behind: snath + blade + head };
  },
};
