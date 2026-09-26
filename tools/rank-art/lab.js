// Badge workbench: renders the redesigned badges (rkn-*) through the real sprite with depth on.
//   node tools/rank-art/lab.js --ranks 3 --out shots/lab_r3.png --port 9403 [--size 170] [--shipped]
//     one or more ranks: each gets d1 d2 d3 large on the dark panel, the same on the red hero,
//     and the three at 56 / 36 / 20 px (hero, ladder chip, leaderboard) on panel and red.
//     --shipped adds the as-shipped badges of that rank for comparison.
//   node tools/rank-art/lab.js --ladder --out shots/ladder.png --port 9410
//     the whole 25-badge ladder at 48 px and 20 px, on panel and on red - for comparing ranks.
const fs = require("fs");
const path = require("path");
const { launch, sleep } = require("./cdp");
const { build } = require("./compose");

const args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf(k); return i < 0 ? d : args[i + 1]; };
const has = k => args.includes(k);
const OUT = path.resolve(opt("--out", path.join(require("os").tmpdir(), "rank-art-lab.png")));
const PORT = +opt("--port", 9400);
const SIZE = +opt("--size", 170);
const ranks = (opt("--ranks", "") || "").split(",").map(Number).filter(Boolean);
const names = ["", "Rookie", "Private", "Soldier", "Veteran", "Operator", "Shadow", "Nightmare", "Spectre", "Reaper"];
const pad = n => String(n).padStart(2, "0");
const idsOf = r => r === 9 ? [25] : [1, 2, 3].map(d => (r - 1) * 3 + d);
const svg = (id, px) => `<svg viewBox="0 0 80 92" style="width:${px}px;height:${Math.round(px * 92 / 80)}px;flex:none;--rk-depth:1;filter:drop-shadow(0 ${px > 60 ? 6 : 3}px ${px > 60 ? 9 : 4}px rgba(0,0,0,.55)) drop-shadow(0 1px 1px rgba(0,0,0,.4))"><use href="#${id}"></use></svg>`;

let bodyHtml = "", width = 1200, height = 200;
if (has("--ladder")) {
  const all = [];
  for (let r = 1; r <= 9; r++) all.push(...idsOf(r));
  const row = (px, bg) => `<div class="band" style="background:${bg}">${all.map(n => svg("rkn-" + pad(n), px)).join("")}</div>`;
  bodyHtml = `<div class="lbl">${names.slice(1).map((n, i) => `<span style="width:${i === 8 ? 52 : 52 * 3}px">${n}</span>`).join("")}</div>`
    + row(48, "#15151a") + row(48, "#c8102e") + row(20, "#15151a") + row(20, "#c8102e") + row(36, "#1e1e23");
  width = 25 * 52 + 60; height = 520;
} else {
  for (const r of ranks) {
    const ids = idsOf(r).map(n => "rkn-" + pad(n));
    const big = bg => `<div class="cell" style="background:${bg}">${ids.map(id => svg(id, SIZE)).join("")}</div>`;
    const small = bg => `<div class="cell small" style="background:${bg}">${[56, 36, 20].map(px => ids.map(id => svg(id, px)).join("")).join('<span class="gap"></span>')}</div>`;
    const shipped = has("--shipped") ? `<div class="cell small" style="background:#15151a"><span class="tag">as shipped</span>${idsOf(r).map(n => svg("rk-" + pad(n), 56)).join("")}</div>` : "";
    bodyHtml += `<h2>${names[r]}</h2><div class="row">${big("#15151a")}${big("#c8102e")}</div><div class="row">${small("#15151a")}${small("#c8102e")}${shipped}</div>`;
  }
  width = Math.max(1200, (ids => 2 * (ids * (SIZE + 14)) + 60)(ranks.includes(9) && ranks.length === 1 ? 1 : 3));
  height = ranks.length * (SIZE * 1.15 + 190) + 20;
}

const html = `<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;padding:10px;background:#0b0b0c;color:#f4f2f0;font:600 13px system-ui}
h2{margin:6px 0 6px;font:700 15px system-ui;letter-spacing:.06em;text-transform:uppercase;color:#aaa}
.row{display:flex;gap:10px;margin-bottom:8px;align-items:stretch}
.cell{display:flex;gap:14px;align-items:center;padding:14px}
.cell.small{gap:8px}
.gap{width:18px}
.tag{color:#77777d;font-size:11px;margin-right:6px}
.band{display:flex;gap:4px;align-items:center;padding:8px 10px}
.lbl{display:flex;gap:0;padding:0 10px;color:#77777d;font-size:11px}
.lbl span{flex:none}
</style></head><body>
<svg width="0" height="0" style="position:absolute;width:0;height:0;overflow:hidden" aria-hidden="true"><defs>${build(has("--ladder") ? {} : { only: ranks })}</defs></svg>
${bodyHtml}</body></html>`;
const htmlFile = OUT.replace(/\.png$/, ".html");
fs.mkdirSync(path.dirname(OUT), { recursive: true });
fs.writeFileSync(htmlFile, html);
(async () => {
  const b = await launch({ port: PORT, width, height: Math.round(height) });
  try {
    await b.goto("file:///" + htmlFile.replace(/\\/g, "/"));
    await sleep(400);
    await b.shot(OUT);
    console.log("wrote", OUT, width + "x" + Math.round(height), b.logs.length ? "\n" + b.logs.join("\n") : "");
  } finally { await b.close(); }
})().catch(e => { console.error(e); process.exit(1); });
