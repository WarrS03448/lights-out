// Veteran - battle-hardened and decorated; the top of the promotion ladder.
// Glyph: a faceted star medal hanging from a striped suspension ribbon.
// Evolution (honours / laurels): d2 laurel sprigs climb the plate's sides; d3 a full laurel wreath
// that meets in a ribbon bow below the plate, with a star above it.
const f = v => +v.toFixed(2);
const pt = (x, y) => f(x) + "," + f(y);

// ---------------------------------------------------------------- glyph (64 x 64)
// Suspension ribbon: a pentagon block (flat top, V bottom) with dark stripes clipped to it.
// The star is the medal and fills most of the glyph; the ribbon is only a short tab above it, so
// at 20 px the silhouette is a star with a tag on top rather than two blocks (an hourglass).
const RIB = { l: 22, r: 42, top: 2.8, sideY: 10.4, tipY: 14.6 };
const ribBottom = x => x <= 32 ? RIB.sideY + (x - RIB.l) * (RIB.tipY - RIB.sideY) / (32 - RIB.l)
                               : RIB.tipY - (x - 32) * (RIB.tipY - RIB.sideY) / (RIB.r - 32);
const stripe = (a, b) => `M${pt(a, RIB.top)} L${pt(b, RIB.top)} L${pt(b, ribBottom(b))} ${a < 32 && b > 32 ? `L${pt(32, RIB.tipY)} ` : ""}L${pt(a, ribBottom(a))} Z`;
const RING = { cy: 16.4, r: 2 };
const STAR_R = 23.6, STAR_r = 10.3, STAR_C = [32, RING.cy + RING.r + STAR_R - 0.2];
function starPts(cx, cy, R, r) {
  const o = [], i = [];
  for (let k = 0; k < 5; k++) {
    const a = (-90 + k * 72) * Math.PI / 180, b = (-90 + 36 + k * 72) * Math.PI / 180;
    o.push([cx + R * Math.cos(a), cy + R * Math.sin(a)]);
    i.push([cx + r * Math.cos(b), cy + r * Math.sin(b)]);
  }
  return { o, i };
}
function glyph() {
  const [cx, cy] = STAR_C, { o, i } = starPts(cx, cy, STAR_R, STAR_r);
  const outline = o.map((p, k) => pt(...p) + " " + pt(...i[k])).join(" ");
  // the right half of every arm in shade: a bevelled, struck-metal star
  let facets = "";
  for (let k = 0; k < 5; k++) facets += `M${pt(cx, cy)} L${pt(...o[k])} L${pt(...i[k])} Z `;
  return `<symbol id="gl-4n" viewBox="0 0 64 64">`
    // ribbon block + pin bar
    + `<path d="M${pt(RIB.l, RIB.top)} L${pt(RIB.r, RIB.top)} L${pt(RIB.r, RIB.sideY)} L${pt(32, RIB.tipY)} L${pt(RIB.l, RIB.sideY)} Z" fill="currentColor"></path>`
    + `<path d="${stripe(24.6, 26.8)} ${stripe(37.2, 39.4)} ${stripe(30.3, 33.7)}" fill="#000" opacity=".5"></path>`
    + `<rect x="21" y="1.4" width="22" height="2.6" fill="currentColor"></rect>`
    // suspension ring
    + `<circle cx="32" cy="${RING.cy}" r="${RING.r}" fill="none" stroke="currentColor" stroke-width="2"></circle>`
    // the star: one bright struck silhouette, its facets only a light bevel
    + `<polygon points="${outline}" fill="currentColor"></polygon>`
    + `<path d="${facets.trim()}" fill="#000" opacity=".12"></path>`
    // a small star struck into the centre: a star within a star, the decorated soldier's medal
    + `<polygon points="${(({ o: so, i: si }) => so.map((p, k) => pt(...p) + " " + pt(...si[k])).join(" "))(starPts(cx, cy + 0.4, 5.8, 2.5))}" fill="#000" opacity=".2"></polygon>`
    + `</symbol>`;
}


// ---------------------------------------------------------------- laurels
// The wreath stem is an arc of an ellipse round the plate; theta 0 = straight below its centre,
// 90 = level with it on the left, 180 = straight above. Leaves are set at even arc-length steps,
// in pairs, and grow toward the tip. Each leaf is its own element so the lower ones overlap the
// upper ones like shingles. Drawn for the left side; ctx.both() mirrors it.
// leaf lighting: lit-half opacity 1 at or above y0, falling to lo at y1 and below; outlines fall to edge
const LIT = { y0: 24, y1: 58, lo: 0.22, edge: 0.72 };
function laurel(ctx, { rx, ry, cx = 40, cy = 46, t0, t1, n, L, w, ang = 36, taper = 0.25, tipLeaf = true }) {
  const { P, I } = ctx;
  const R = Math.PI / 180;
  const at = t => [cx - rx * Math.sin(t * R), cy + ry * Math.cos(t * R)];
  const tan = t => { const dx = -rx * Math.cos(t * R), dy = -ry * Math.sin(t * R), m = Math.hypot(dx, dy); return [dx / m, dy / m]; };
  // arc-length table
  const tbl = [[t0, 0]];
  for (let k = 1; k <= 400; k++) {
    const t = t0 + (t1 - t0) * k / 400, a = at(t), b = at(tbl[k - 1][0]);
    tbl.push([t, tbl[k - 1][1] + Math.hypot(a[0] - b[0], a[1] - b[1])]);
  }
  const total = tbl[tbl.length - 1][1];
  const tAt = s => { for (const [t, l] of tbl) if (l >= s) return t; return t1; };
  // stem
  let stem = "M" + pt(...at(t0));
  const segs = 10;
  for (let s = 1; s <= segs; s++) {
    const ta = t0 + (t1 - t0) * (s - 1) / segs, tb = t0 + (t1 - t0) * s / segs;
    const A = at(ta), B = at(tb), da = tan(ta), db = tan(tb), k = (tb - ta) * R / 3;
    const la = Math.hypot(rx * Math.cos(ta * R), ry * Math.sin(ta * R)) * k;
    const lb = Math.hypot(rx * Math.cos(tb * R), ry * Math.sin(tb * R)) * k;
    stem += ` C${pt(A[0] + da[0] * la, A[1] + da[1] * la)} ${pt(B[0] - db[0] * lb, B[1] - db[1] * lb)} ${pt(...B)}`;
  }
  const rot = (v, deg) => { const a = deg * R; return [v[0] * Math.cos(a) - v[1] * Math.sin(a), v[0] * Math.sin(a) + v[1] * Math.cos(a)]; };
  // one leaf: base b, unit direction dir; returns { d, half } where half is the side toward `side`
  const leaf = (b, dir, len, wid, side) => {
    const tip = [b[0] + dir[0] * len, b[1] + dir[1] * len], m = [b[0] + dir[0] * len * 0.45, b[1] + dir[1] * len * 0.45];
    const nx = -dir[1], ny = dir[0];
    const c1 = [m[0] + nx * wid * 1.9, m[1] + ny * wid * 1.9], c2 = [m[0] - nx * wid * 1.9, m[1] - ny * wid * 1.9];
    const d = `M${pt(...b)} Q${pt(...c1)} ${pt(...tip)} Q${pt(...c2)} ${pt(...b)} Z`;
    const cs = side > 0 ? c1 : c2;
    const half = `M${pt(...b)} Q${pt(...cs)} ${pt(...tip)} Z`;
    return { d, half };
  };
  const els = [];
  for (let j = 0; j < n; j++) {
    const s = total * (j + 0.35) / (n + (tipLeaf ? 0.2 : 0)), t = tAt(s), b = at(t), d = tan(t);
    const k = 1 - taper * j / Math.max(1, n - 1);
    const out = leaf(b, rot(d, -ang), L * k, w * k, 1);
    const inn = leaf(b, rot(d, ang), L * k * 0.92, w * k * 0.92, -1);
    els.push([out, inn].map(lf => ({ ...lf, y: b[1] })));
  }
  if (tipLeaf) {
    const e = at(t1), de = tan(t1), k = 1 - taper;
    els.push([{ ...leaf(e, de, L * k * 0.95, w * k * 0.9, 1), y: e[1] }]);
  }
  let body = ctx.both(stem, `fill="none" stroke="${I}" stroke-width="1.4" stroke-linecap="round"`);
  // tip first, so every lower pair is drawn over the one above it
  for (const pair of els.reverse()) {
    for (const lf of pair) {
      // plate-filled leaf, ink outline, the lit half struck in brass: solid where the light falls
      // (the top of the wreath), fading toward the bottom, so the wreath reads as separate leaves
      // and stays darker than a solid brass ring
      const shade = Math.min(1, Math.max(0, (lf.y - LIT.y0) / (LIT.y1 - LIT.y0)));
      const lit = f(1 - (1 - LIT.lo) * shade), edge = f(1 - (1 - LIT.edge) * shade);
      body += ctx.both(lf.d, `fill="${P}" stroke="${I}" stroke-width="1.1" stroke-linejoin="round"${edge < 1 ? ` stroke-opacity="${edge}"` : ""}`)
        + ctx.both(lf.half, `fill="${I}"${lit < 1 ? ` opacity="${lit}"` : ""}`);
    }
  }
  return body;
}

module.exports = {
  rank: 4,
  name: "Veteran",
  plate: "oklch(.27 .02 250)",
  ink: "oklch(.79 .054 72)",
  glyph: glyph(),
  glyphBox: [19, 23, 42, 42],
  plateMod(plate, ctx) {
    // the shoulder rivets become two small engraved stars; the lower-face shade drops from y 46 to
    // y 50, because at 46 its edge carried on the star's horizontal arm line and the arms seemed to
    // sit on a rail
    return plate
      .replace(`<polygon points="16,46 64,46 64,54.1 40,74 16,54.1"`, `<polygon points="16,50 64,50 64,54.1 40,74 16,54.1"`)
      .replace(`<circle cx="24" cy="36" r="1.8" fill="${ctx.I}" opacity=".6"></circle>`, `<polygon points="${ctx.star(24, 36.4, 2.8, 1.15, 5)}" fill="${ctx.I}" opacity=".7"></polygon>`)
      .replace(`<circle cx="56" cy="36" r="1.8" fill="${ctx.I}" opacity=".6"></circle>`, `<polygon points="${ctx.star(56, 36.4, 2.8, 1.15, 5)}" fill="${ctx.I}" opacity=".7"></polygon>`);
  },
  ornaments(ctx) {
    const { P, I, div } = ctx;
    if (div === 2) {
      return { behind: laurel(ctx, { rx: 32.4, ry: 34.5, t0: 46, t1: 127, n: 6, L: 8.2, w: 2.4, ang: 38 }) };
    }
    if (div === 3) {
      const wreath = laurel(ctx, { rx: 33.6, ry: 39, t0: 10, t1: 150, n: 11, L: 9.8, w: 2.8, taper: 0.3 });
      // the wreath is bound below the plate by a bow of the medal's own brass ribbon
      const r = `fill="${I}" stroke="${P}" stroke-width=".6" stroke-linejoin="round"`;
      const bow = ctx.both("M38.4,84.2 L32.6,91.2 L35.3,90 L36.6,91.8 L41,85.8 Z", r)
        + ctx.both("M38.4,84.2 L32.6,91.2 L35.3,90 L36.6,91.8 L41,85.8 Z", `fill="#000" opacity=".22"`)
        + ctx.both("M38.2,82.6 C33.8,77.4 27.4,77.6 27.6,82.2 C27.8,86.6 33.8,86.4 38.2,84.4 Z", r)
        + ctx.both("M37,83.1 C33.4,80.6 29.9,80.8 30,82.5 C30.1,84.2 33.4,84.3 37,83.6 Z", `fill="#000" opacity=".42"`)
        + `<path d="M37.2,80.6 L42.8,80.6 L43.2,86.2 L36.8,86.2 Z" ${r}></path>`;
      const top = `<polygon points="${ctx.star(40, 7.2, 7, 2.9, 5)}" fill="${I}" stroke="${P}" stroke-width=".6" stroke-linejoin="round"></polygon>`;
      return { behind: wreath + top, front: bow };
    }
    return {};
  },
};
