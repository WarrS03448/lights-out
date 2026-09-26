// Rank badge composer. Builds the sprite body (everything that goes inside the page's <defs>):
//   - the original 9 glyphs and 25 badges (ids gl-1..gl-9, rk-01..rk-25), each badge given the
//     depth lighting group - these are "as shipped" and must stay byte-for-byte the same art;
//   - the redesigned badges rkn-01..rkn-25, one module per rank in ./ranks/r<rank>.js.
//
// A rank module exports:
//   { rank, name,
//     plate, ink,                     // oklch() strings: the plate fill and the ink (strokes, glyph)
//     glyph,                          // one <symbol id="gl-<rank>n" viewBox="0 0 64 64">...</symbol>
//     defs,                           // optional extra <mask>/<linearGradient>... ids MUST start "r<rank>-"
//     glyphBox: [x, y, w, h],         // optional, default [20, 22, 40, 40] (Reaper used [18, 20, 44, 44])
//     plateMod(plateMarkup, ctx),     // optional: edit the plate (machining, shading) and return it
//     ornaments(ctx) -> { behind, front, over },   // the division evolution (ctx.div 1..3, 0 = capstone)
//     glyphExtra(ctx) -> markup }     // optional: extra <use>s drawn just under the glyph's own shadow
//
// ctx = { rank, div, n, plate, ink, P: plate, I: ink, mirror(d), both(d, attrs), star(cx, cy, R, r, points) }
// Coordinate space of a badge: viewBox 0 0 80 92. The plate is the fixed hex shield
//   40,14 68,28.1 68,56.2 40,78 12,56.2 12,28.1   (face inset: 40,18 64,30.1 64,54.1 40,74 16,54.1 16,30.1)
// so ornaments live in x 0-12 / 68-80 (sides), y 0-14 (top) and y 78-92 (bottom).
// Layers: behind (under the plate) -> plate -> front -> lighting -> glyph shadow/emboss/glyph -> over.
const fs = require("fs");
const path = require("path");

// The 2.8.6 (as-shipped) sprite: the redesign keeps each rank's plate machining from it.
const ORIG = fs.readFileSync(path.join(__dirname, "plates.svgfrag"), "utf8");
const FACE = "40,18 64,30.1 64,54.1 40,74 16,54.1 16,30.1";
const PLATE = "40,14 68,28.1 68,56.2 40,78 12,56.2 12,28.1";
// Every division ornament the original art draws (spurs, their highlights, crown, spike, the
// capstone's outer ring) - stripped so each rank can draw its own evolution.
const ORNAMENT_POINTS = [
  "68,32 80,40 68,48", "68,32 80,40 68,40", "12,32 0,40 12,48", "12,32 0,40 12,40",
  "68,28 80,40 68,52", "68,28 80,40 68,40", "12,28 0,40 12,52", "12,28 0,40 12,40",
  "26,13 40,1 54,13 40,8", "33,72 40,90 47,72", "40,90 47,72 40,74",
  "68,26 80,40 68,54", "68,26 80,40 68,40", "12,26 0,40 12,54", "12,26 0,40 12,40",
  "24,12 40,0 56,12 40,6", "32,72 40,92 48,72", "40,92 48,72 40,74",
  "40,9 74,26 74,58 40,83 6,58 6,26",
];

const DEFS = `<radialGradient id="rkd-dome" cx=".36" cy=".18" r=".98"><stop offset="0" stop-color="#fff" stop-opacity=".24"></stop><stop offset=".45" stop-color="#fff" stop-opacity="0"></stop><stop offset="1" stop-color="#000" stop-opacity=".34"></stop></radialGradient>`;

function symbols(sprite) {
  const out = {};
  for (const m of sprite.matchAll(/<symbol id="([^"]+)"[\s\S]*?<\/symbol>/g)) out[m[1]] = m[0];
  return out;
}

const LIGHTING = `<g style="opacity:var(--rk-depth,0)">`
  + `<polygon points="${FACE}" fill="url(#rkd-dome)"></polygon>`
  + `<polygon points="40,78 68,56.2 64,54.1 40,74 16,54.1 12,56.2" fill="#000" opacity=".22"></polygon>`
  + `<polygon points="40,14 68,28.1 64,30.1 40,18 16,30.1 12,28.1" fill="#fff" opacity=".12"></polygon>`
  + `<polyline points="12.9,28.4 40,14.9 67.1,28.4" fill="none" stroke="#fff" stroke-opacity=".55" stroke-width=".6"></polyline>`
  + `</g>`;

// The lighting group on an original (as-shipped) badge, plus its glyph's embossed top edge.
function lightOriginal(sym) {
  const glyphUse = sym.match(/<use href="#(gl-[\w]+)" x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)" style="color:(?!#000)[^"]*"><\/use>/);
  const first = sym.indexOf('<use href="#gl-');
  let out = sym.slice(0, first) + LIGHTING + sym.slice(first);
  if (glyphUse) {
    const [whole, gl, x, y, w, h] = glyphUse;
    out = out.replace(whole, `<use href="#${gl}" x="${(+x - 0.8).toFixed(1)}" y="${(+y - 0.9).toFixed(1)}" width="${w}" height="${h}" style="color:#fff;opacity:calc(var(--rk-depth,0) * .32);--rk-accent:transparent"></use>` + whole);
  }
  return out;
}

// ---------------------------------------------------------------- helpers for modules
function mirror(d) {
  // Mirror absolute path data about x = 40. Supports M L H V C S Q T Z (absolute only).
  const tokens = d.match(/[MLHVCSQTZmlhvcsqtz]|-?\d*\.?\d+(?:e-?\d+)?/g) || [];
  let out = "", cmd = "", argi = 0;
  for (const t of tokens) {
    if (/[A-Za-z]/.test(t)) {
      if (t !== t.toUpperCase()) throw new Error("mirror(): relative commands are not supported: " + t);
      if (t === "A") throw new Error("mirror(): arcs are not supported");
      cmd = t; argi = 0; out += " " + t; continue;
    }
    const v = parseFloat(t);
    let r = v;
    if (cmd === "H") r = 80 - v;
    else if (cmd !== "V" && argi % 2 === 0) r = 80 - v;
    out += (argi === 0 ? " " : (argi % 2 === 1 ? "," : " ")) + +r.toFixed(3);
    argi++;
  }
  return out.trim();
}
function star(cx, cy, R, r, points = 5, rot = -90) {
  const pts = [];
  for (let i = 0; i < points * 2; i++) {
    const a = (rot + i * 180 / points) * Math.PI / 180, rr = i % 2 ? r : R;
    pts.push((cx + rr * Math.cos(a)).toFixed(2) + "," + (cy + rr * Math.sin(a)).toFixed(2));
  }
  return pts.join(" ");
}

function ctxFor(mod, div, n) {
  const P = mod.plate, I = mod.ink;
  return {
    rank: mod.rank, div, n, plate: P, ink: I, P, I, mirror, star,
    // the same path on both sides: <path d="d"/> and its mirror, sharing attrs
    both: (d, attrs) => `<path d="${d}" ${attrs}></path><path d="${mirror(d)}" ${attrs}></path>`,
  };
}

function stripPlate(sym) {
  let body = sym.replace(/^<symbol[^>]*>/, "").replace(/<\/symbol>$/, "");
  for (const p of ORNAMENT_POINTS) {
    body = body.split(new RegExp(`<polygon points="${p.replace(/\./g, "\\.")}"[^>]*></polygon>`)).join("");
  }
  body = body.replace(/<use href="#gl-\d"[^>]*><\/use>/g, "");
  return body;
}

function recolor(body, orig, mod) {
  const m = orig.match(new RegExp(`<polygon points="${PLATE}" fill="([^"]+)" stroke="([^"]+)"`));
  if (!m) throw new Error("no plate in original");
  return body.split(m[1]).join(mod.plate).split(m[2]).join(mod.ink);
}

function compose(mod, orig, n, div, prefix = "rkn-") {
  const ctx = ctxFor(mod, div, n);
  let plate = recolor(stripPlate(orig), orig, mod);
  if (mod.plateMod) plate = mod.plateMod(plate, ctx);
  const orn = (mod.ornaments ? mod.ornaments(ctx) : null) || {};
  const [x, y, w, h] = mod.glyphBox || [20, 22, 40, 40];
  const gid = `gl-${mod.rank}n`;
  const glyph = (mod.glyphExtra ? mod.glyphExtra(ctx) : "")
    + `<use href="#${gid}" x="${x + 1.4}" y="${y + 1.6}" width="${w}" height="${h}" style="color:#000;opacity:.5;--rk-accent:transparent"></use>`
    + `<use href="#${gid}" x="${(x - 0.8).toFixed(1)}" y="${(y - 0.9).toFixed(1)}" width="${w}" height="${h}" style="color:#fff;opacity:calc(var(--rk-depth,0) * .32);--rk-accent:transparent"></use>`
    + `<use href="#${gid}" x="${x}" y="${y}" width="${w}" height="${h}" style="color:${mod.ink}"></use>`;
  const id = prefix + String(n).padStart(2, "0");
  return `<symbol id="${id}" viewBox="0 0 80 92">${orn.behind || ""}${plate}${orn.front || ""}${LIGHTING}${glyph}${orn.over || ""}</symbol>`;
}

function loadModules(only) {
  const mods = {};
  for (let r = 1; r <= 9; r++) {
    if (only && !only.includes(r)) continue;
    const f = path.join(__dirname, "ranks", `r${r}.js`);
    delete require.cache[require.resolve(f)];
    const m = require(f);
    if (m.rank !== r) throw new Error(`r${r}.js exports rank ${m.rank}`);
    if (!new RegExp(`<symbol id="gl-${r}n" viewBox="0 0 64 64">`).test(m.glyph || "")) throw new Error(`r${r}.js glyph must be <symbol id="gl-${r}n" viewBox="0 0 64 64">`);
    for (const idm of ((m.defs || "") + (m.glyph || "")).matchAll(/ id="([^"]+)"/g)) {
      const id = idm[1];
      if (id !== `gl-${r}n` && !id.startsWith(`r${r}-`)) throw new Error(`r${r}.js: id "${id}" must start with "r${r}-"`);
    }
    mods[r] = m;
  }
  return mods;
}

// The whole sprite body: shared defs, originals (lit), module glyphs/defs, redesigned badges.
// opts.only: rank numbers to compose (others are left out) - lets one rank be worked on while
// another rank's module is mid-edit.
function build(opts = {}) {
  const syms = symbols(ORIG);
  const mods = loadModules(opts.only);
  const masks = ORIG.slice(0, ORIG.indexOf("<symbol"));
  let body = masks + DEFS;
  for (const r of Object.keys(mods)) body += (mods[r].defs || "") + mods[r].glyph;
  // opts.production: the sprite the hub ships - the redesigned badges under the real ids rk-01..25,
  // and none of the as-shipped art.
  if (!opts.production) for (const id of Object.keys(syms)) body += id.startsWith("rk-") ? lightOriginal(syms[id]) : syms[id];
  for (let n = 1; n <= 25; n++) {
    const rank = n === 25 ? 9 : Math.floor((n - 1) / 3) + 1;
    const div = n === 25 ? 0 : n - (rank - 1) * 3;
    if (!mods[rank]) continue;
    body += compose(mods[rank], syms["rk-" + String(n).padStart(2, "0")], n, div, opts.production ? "rk-" : "rkn-");
  }
  const ids = [...body.matchAll(/ id="([^"]+)"/g)].map(m => m[1]);
  const dup = ids.filter((v, i) => ids.indexOf(v) !== i);
  if (dup.length) throw new Error("duplicate ids in sprite: " + [...new Set(dup)].join(", "));
  return body;
}

// Helpers the placeholder modules use to redraw the original evolution.
function genericOrnaments(ctx, opacity = 1) {
  const { P, I, div } = ctx, op = opacity < 1 ? ` opacity="${opacity}"` : "";
  if (div === 2) return { behind: `<polygon points="68,32 80,40 68,48" fill="${P}" stroke="${I}" stroke-width="1.4"${op}></polygon><polygon points="12,32 0,40 12,48" fill="${P}" stroke="${I}" stroke-width="1.4"${op}></polygon>` };
  if (div === 3) return { behind: `<polygon points="68,28 80,40 68,52" fill="${P}" stroke="${I}" stroke-width="1.4"${op}></polygon><polygon points="12,28 0,40 12,52" fill="${P}" stroke="${I}" stroke-width="1.4"${op}></polygon><polygon points="26,13 40,1 54,13 40,8" fill="${P}" stroke="${I}" stroke-width="1.4"${op}></polygon><polygon points="33,72 40,90 47,72" fill="${P}" stroke="${I}" stroke-width="1.4"${op}></polygon>` };
  return {};
}

module.exports = { build, compose, loadModules, mirror, star, genericOrnaments, FACE, PLATE, ORIG, symbols };
