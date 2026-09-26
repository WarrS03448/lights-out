// Nightmare - what the other team calls that. APPROVED by Sam 2026-09-25: keep as is.
// Glyph: a horned thing with a slit eye and teeth. Plate: a jaw of teeth round the lower rim.
// Evolution: d2 the side spurs become curved horns; d3 bigger horns, twin devil horns on top, a fang below.
const FACE_EDGES = [[[16, 54.1], [40, 74], 1], [[40, 74], [64, 54.1], -1]];

function jaw(ink) {
  let d = "";
  for (const [[x1, y1], [x2, y2], side] of FACE_EDGES) {
    const len = Math.hypot(x2 - x1, y2 - y1), ux = (x2 - x1) / len, uy = (y2 - y1) / len;
    const nx = side > 0 ? uy : -uy, ny = side > 0 ? -ux : ux;
    for (const t of [0.16, 0.38, 0.62, 0.84]) {
      const bx = x1 + (x2 - x1) * t, by = y1 + (y2 - y1) * t, hw = 1.9, tip = 4.6;
      const p = [[bx - ux * hw, by - uy * hw], [bx + nx * tip, by + ny * tip], [bx + ux * hw, by + uy * hw]];
      d += "M" + p.map(q => q[0].toFixed(2) + "," + q[1].toFixed(2)).join(" L") + " Z ";
    }
  }
  return `<path d="${d.trim()}" fill="${ink}" opacity=".78"></path>`;
}

module.exports = {
  rank: 7,
  name: "Nightmare",
  plate: "oklch(.155 .04 18)",
  ink: "oklch(.79 .028 72)",
  glyph: `<symbol id="gl-7n" viewBox="0 0 64 64">`
    + `<path d="M24.5,23 C17.4,19.8 12.6,13.2 12.2,3.6 C15.8,9.6 20.9,12.8 28.6,15.4 Z" fill="currentColor"></path>`
    + `<path d="M39.5,23 C46.6,19.8 51.4,13.2 51.8,3.6 C48.2,9.6 43.1,12.8 35.4,15.4 Z" fill="currentColor"></path>`
    + `<path d="M6.5,35.5 C16,23.6 48,23.6 57.5,35.5 C48,47.4 16,47.4 6.5,35.5 Z" fill="currentColor"></path>`
    + `<circle cx="32" cy="35.5" r="9.6" fill="#000" opacity=".42"></circle>`
    + `<path d="M32,25.4 C35.6,29.6 35.6,41.4 32,45.6 C28.4,41.4 28.4,29.6 32,25.4 Z" fill="#000" opacity=".94"></path>`
    + `<path d="M22.6,43.9 L25.1,53.4 L27.8,45.1 Z M29.7,45.6 L32,57.2 L34.3,45.6 Z M36.2,45.1 L38.9,53.4 L41.4,43.9 Z" fill="currentColor"></path>`
    + `</symbol>`,
  plateMod(plate, ctx) {
    const face = plate.indexOf(`<polygon points="40,18 64,30.1 64,54.1 40,74 16,54.1 16,30.1" fill="none"`);
    return plate.slice(0, face) + jaw(ctx.I) + plate.slice(face);
  },
  ornaments(ctx) {
    const { P, I, div } = ctx;
    const a = `fill="${P}" stroke="${I}" stroke-width="1.4" stroke-linejoin="round"`;
    if (div === 2) return { behind: ctx.both("M68,33 C73.4,33 78,29.8 80,23.6 C80.4,33 75.4,41.2 68,47 Z", a) };
    if (div === 3) return {
      behind: ctx.both("M68,29 C74.2,28.8 78.8,23.8 80,14.8 C81.4,28.4 76.4,42.2 68,51 Z", a)
        + `<path d="M28.4,15 C25.6,9.6 25.4,4.4 28.6,0.2 C29.2,5.2 31.8,9 36.2,11.6 Z M51.6,15 C54.4,9.6 54.6,4.4 51.4,0.2 C50.8,5.2 48.2,9 43.8,11.6 Z" ${a}></path>`
        + `<polygon points="33,72 40,90 47,72" fill="${P}" stroke="${I}" stroke-width="1.4"></polygon><polygon points="40,90 47,72 40,74" fill="#000" opacity=".3"></polygon>`,
    };
    return {};
  },
};
