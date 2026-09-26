// Rookie - a fresh recruit, the first rung: humble, new, un-decorated.
// Glyph: a pair of dog tags on a ball chain - the only thing a new recruit is issued.
// Plate: a cloth patch - the machined inner bezel becomes a running stitch.
// Evolution (cloth and stitching): d2 stitched fabric tabs sewn onto the sides; d3 bigger tabs,
// a tag ring on top and a swallowtail cloth ribbon banner across the bottom.
const f = n => +n.toFixed(2);
const K = 0.5523;

// a circle as four cubics (absolute, no arcs)
function circ(cx, cy, r) {
  const k = r * K;
  return `M${f(cx)},${f(cy - r)} C${f(cx + k)},${f(cy - r)} ${f(cx + r)},${f(cy - k)} ${f(cx + r)},${f(cy)}`
    + ` C${f(cx + r)},${f(cy + k)} ${f(cx + k)},${f(cy + r)} ${f(cx)},${f(cy + r)}`
    + ` C${f(cx - k)},${f(cy + r)} ${f(cx - r)},${f(cy + k)} ${f(cx - r)},${f(cy)}`
    + ` C${f(cx - r)},${f(cy - k)} ${f(cx - k)},${f(cy - r)} ${f(cx)},${f(cy - r)} Z`;
}

// a rounded rectangle turned deg about (px, py) - positive turns clockwise on screen, since y
// points down; returned as absolute M/L/C path data
function rrect(x, y, w, h, r, deg = 0, px = x, py = y) {
  const a = deg * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
  const T = (X, Y) => { const dx = X - px, dy = Y - py; return f(px + dx * c - dy * s) + "," + f(py + dx * s + dy * c); };
  const k = r * (1 - K), X2 = x + w, Y2 = y + h;
  return `M${T(x + r, y)} L${T(X2 - r, y)} C${T(X2 - k, y)} ${T(X2, y + k)} ${T(X2, y + r)}`
    + ` L${T(X2, Y2 - r)} C${T(X2, Y2 - k)} ${T(X2 - k, Y2)} ${T(X2 - r, Y2)}`
    + ` L${T(x + r, Y2)} C${T(x + k, Y2)} ${T(x, Y2 - k)} ${T(x, Y2 - r)}`
    + ` L${T(x, y + r)} C${T(x, y + k)} ${T(x + k, y)} ${T(x + r, y)} Z`;
}

// ---- the glyph (64 x 64)
const TW = 23, TH = 38, TR = 6.4;
// each tag hangs from its hole; the front one swings a little left, the back one right
const FRONT = { x: 15, y: 18.5, deg: 7 };
const BACK = { x: 22, y: 16.5, deg: -16 };
const holeOf = t => [t.x + TW / 2, t.y + 5];
const tagPath = t => { const [hx, hy] = holeOf(t); return rrect(t.x, t.y, TW, TH, TR, t.deg, hx, hy); };
const rimPath = t => { const [hx, hy] = holeOf(t); return rrect(t.x + 2.3, t.y + 2.3, TW - 4.6, TH - 4.6, TR - 2.3, t.deg, hx, hy); };
// a tag turns about its own hole, so the holes stay put
const frontHole = holeOf(FRONT), backHole = holeOf(BACK);

// the ball chain: one closed loop through both holes, beads spaced evenly along its length
function bez(p0, p1, p2, p3, t) {
  const u = 1 - t;
  return [0, 1].map(i => u * u * u * p0[i] + 3 * u * u * t * p1[i] + 3 * u * t * t * p2[i] + t * t * t * p3[i]);
}
// returns [beads, necks]: the balls, and a small dark pinch between each pair of balls
function beadChain(segs, gap, r) {
  const pts = [];
  for (const s of segs) for (let i = pts.length ? 1 : 0; i <= 80; i++) pts.push(bez(...s, i / 80));
  const at = [pts[0]];
  let run = 0;
  for (let i = 1; i < pts.length; i++) {
    run += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    if (run >= gap) { at.push(pts[i]); run = 0; }
  }
  const beads = at.map(p => circ(...p, r)).join(" ");
  const necks = at.slice(1).map((p, i) => circ((p[0] + at[i][0]) / 2, (p[1] + at[i][1]) / 2, r * .3)).join(" ");
  return [beads, necks];
}
const LOOP = [
  [frontHole, [21.4, 17.4], [17.6, 8.6], [24.4, 3.6]],
  [[24.4, 3.6], [28.4, 0.8], [35.6, 0.8], [39.6, 3.6]],
  [[39.6, 3.6], [46.4, 8.6], [42.4, 16.2], backHole],
];
const [CHAIN, NECKS] = beadChain(LOOP, 3.2, 1.55);
// a solid cord under the beads, so the chain reads as one line (and embosses as one) when small
const CORD = "M" + LOOP[0][0].join(",") + " " + LOOP.map(s => "C" + s.slice(1).map(q => q.join(",")).join(" ")).join(" ");

// stamped lines of text on the front tag
function stamp(t) {
  const [hx, hy] = holeOf(t);
  const lines = [[4.4, 15.6, 11.5], [4.4, 12.4, 16.5], [4.4, 14.6, 21.5], [4.4, 9.6, 26.5]];
  return lines.map(([x0, w, dy]) => rrect(t.x + x0, t.y + dy, w, 2, 1, t.deg, hx, hy)).join(" ");
}

const GLYPH = `<symbol id="gl-1n" viewBox="0 0 64 64">`
  + `<path d="${CORD}" fill="none" stroke="currentColor" stroke-width="1.4"></path>`
  + `<path d="${CHAIN}" fill="currentColor"></path>`
  + `<path d="${NECKS}" fill="#000" opacity=".45"></path>`
  + `<g mask="url(#r1-gap)">`
  + `<path d="${tagPath(BACK)}" fill="currentColor"></path>`
  + `<path d="${tagPath(BACK)}" fill="#000" opacity=".26"></path>`
  + `</g>`
  + `<path d="${tagPath(FRONT)}" fill="currentColor"></path>`
  + `<path d="${rimPath(FRONT)}" fill="none" stroke="#000" stroke-width="1.1" opacity=".22"></path>`
  + `<path d="${circ(frontHole[0], frontHole[1], 2.1)}" fill="#000" opacity=".7"></path>`
  + `<path d="${stamp(FRONT)}" fill="#000" opacity=".34"></path>`
  + `</symbol>`;

const DEFS = `<mask id="r1-gap" maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">`
  + `<rect x="0" y="0" width="64" height="64" fill="#fff"></rect>`
  + `<path d="${tagPath(FRONT)}" fill="#000" stroke="#000" stroke-width="4.4" stroke-linejoin="round"></path>`
  + `</mask>`;

// ---- the plate: a sewn cloth patch
const FACE_LINE = /<polygon points="40,18 64,30\.1 64,54\.1 40,74 16,54\.1 16,30\.1" fill="none" stroke="[^"]+" stroke-width="\.8" opacity="\.5"><\/polygon>/;

module.exports = {
  rank: 1,
  name: "Rookie",
  plate: "oklch(.33 .045 78)",
  ink: "oklch(.87 .065 82)",
  defs: DEFS,
  glyph: GLYPH,
  glyphBox: [19.5, 22, 43, 43],
  plateMod(plate, ctx) {
    const stitch = `<polygon points="40,18 64,30.1 64,54.1 40,74 16,54.1 16,30.1" fill="none" stroke="${ctx.I}" stroke-width=".9" stroke-dasharray="2.1 1.5" stroke-linecap="round" opacity=".62"></polygon>`;
    return FACE_LINE.test(plate) ? plate.replace(FACE_LINE, stitch) : plate + stitch;
  },
  ornaments(ctx) {
    const { P, I, div } = ctx;
    const cloth = `fill="${P}" stroke="${I}" stroke-width="1.3" stroke-linejoin="round"`;
    const sew = `fill="none" stroke="${I}" stroke-width=".8" stroke-dasharray="1.6 1.2" stroke-linecap="round" opacity=".8"`;
    // a swallowtailed cloth tab sewn over the plate's side rim: outer end at x o, notch n deep,
    // spanning y t..b; its inner end is tacked down with a column of cross-stitches
    const tab = (o, n, t, b) => {
      const m = (t + b) / 2, x = 15.2;
      let seam = "";
      const k = Math.round((b - t - 4.6) / 3.9);
      for (let i = 0; i <= k; i++) {
        const y = f(t + 2.3 + i * (b - t - 4.6) / k);
        seam += `M13.1,${f(y - .85)} L14.7,${f(y + .85)} M14.7,${f(y - .85)} L13.1,${f(y + .85)} `;
      }
      return ctx.both(`M${x},${t + .5} C11,${t - .1} 7,${t - .1} ${o},${t + .3} L${o + n},${m} L${o},${b - .3} C7,${b + .1} 11,${b + .1} ${x},${b - .5} Z`, cloth)
        + ctx.both(`M${x},${m} L${o + n},${m} L${o},${b - .3} C7,${b + .1} 11,${b + .1} ${x},${b - .5} Z`, `fill="#000" opacity=".2"`)
        + ctx.both(`M12.2,${t + 2.2} C9.8,${t + 1.8} 7.4,${t + 1.8} ${f(o + 2.4)},${t + 2.1} L${f(o + n + 2.2)},${m} L${f(o + 2.4)},${b - 2.1} C7.4,${b - 1.8} 9.8,${b - 1.8} 12.2,${b - 2.2}`, sew)
        + ctx.both(seam.trim(), `fill="none" stroke="${I}" stroke-width=".75" stroke-linecap="round" opacity=".9"`);
    };
    if (div === 2) return { front: tab(2.6, 3.8, 33.6, 46.4) };
    if (div === 3) {
      const cy = 8.2, ring = circ(40, cy, 6.4) + " " + circ(40, cy, 3.5);
      const R = 5.8, r = 4.1, kR = R * K, kr = r * K;
      // the ring's lower half in shade (inside its ink edges), like the tabs and the banner's folds
      const shade = `M${40 + R},${cy} C${40 + R},${f(cy + kR)} ${f(40 + kR)},${cy + R} 40,${cy + R} C${f(40 - kR)},${cy + R} ${40 - R},${f(cy + kR)} ${40 - R},${cy}`
        + ` L${40 - r},${cy} C${40 - r},${f(cy + kr)} ${f(40 - kr)},${cy + r} 40,${cy + r} C${f(40 + kr)},${cy + r} ${40 + r},${f(cy + kr)} ${40 + r},${cy} Z`;
      return {
        // the tag ring: a split ring hung from the plate's crown, clear of the chain below
        behind: `<path d="${ring}" fill="${P}" fill-rule="evenodd" stroke="${I}" stroke-width="1.2"></path>`
          + `<path d="${shade}" fill="#000" opacity=".2"></path>`
          // the banner's tails, folded behind it
          + ctx.both("M22,77 L7,77.4 L10.6,82 L7,86.6 L22,86.8 Z", cloth)
          + ctx.both("M18,82 L22,86.8 L22,82 Z", `fill="#000" opacity=".45"`),
        front: tab(0.2, 4.7, 29, 51),
        over: `<path d="M18,73 Q40,80.5 62,73 L62,82 Q40,89.5 18,82 Z" ${cloth}></path>`
          + `<path d="M20,75.2 Q40,82.2 60,75.2 M60,79.8 Q40,86.8 20,79.8" ${sew}></path>`,
      };
    }
    return {};
  },
};
