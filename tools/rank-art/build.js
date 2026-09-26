// Rebuild the rank badge sprite from the rank modules and write it into the two files that carry
// it: hub/webui/static/ranksprite.js (what the hub loads) and hub/webui/static/ranks.svg (the
// master for design tooling). They must never drift; tests/test_screen_rankbadge.py checks.
//   node tools/rank-art/build.js
// Preview any rank first: node tools/rank-art/lab.js --ranks 3 --out shots/r3.png --shipped
const fs = require("fs");
const path = require("path");
const { build } = require("./compose");

const STATIC = path.join(__dirname, "..", "..", "hub", "webui", "static");
const body = build({ production: true })
  .trim()
  .replace(/(<\/(?:symbol|mask|linearGradient|radialGradient)>)\s*(?=<(?:symbol|mask|linearGradient|radialGradient)[ >])/g, "$1\n") + "\n";
if (/[`\\]|\$\{/.test(body)) throw new Error("the sprite would break the JS template literal it is pasted into");

function splice(file, open, close, text, last) {
  const src = fs.readFileSync(file, "utf8");
  const a = src.indexOf(open) + open.length;
  const b = last ? src.lastIndexOf(close) : src.indexOf(close, a);
  if (a < open.length || b < a) throw new Error("markers not found in " + file);
  fs.writeFileSync(file, src.slice(0, a) + text + src.slice(b));
}
splice(path.join(STATIC, "ranksprite.js"), "var SPRITE = `", "`;", body, false);
splice(path.join(STATIC, "ranks.svg"), "<defs>", "</defs>", "\n" + body, true);
console.log("wrote the rank sprite:", (body.match(/<symbol id="rk-\d\d"/g) || []).length, "badges,", body.length, "bytes");
