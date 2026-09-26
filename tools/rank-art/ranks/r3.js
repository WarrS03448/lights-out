// Soldier - trained combat infantry.
// Glyph: an M1 steel pot seen from the front - a round dome in a woodland camouflage cover, shaded
// on its right, with the cover band round it; the sides drop straight to a thin flat rim (no flare:
// a flared skirt and dipped visor read as a Stahlhelm), chinstrap bales, chinstrap and buckle.
// Plate: field green with a faint two-tone woodland camouflage in the face; the rank's two
// machined dots sit up in the shoulders and the centre line is dropped.
// Evolution (WEAPONS): d1 the bare plate; d2 two rifle muzzles (handguard, front sight, barrel,
// flash hider) angle out from behind the upper sides; d3 the full crossed rifles, a 47-degree X -
// bayonets fixed above the upper corners, swooping stocks with butt plates below the lower ones.

// Rifle geometry, drawn for the rifle whose butt is lower-left and muzzle upper-right; ctx.both()
// mirrors it into the other one. s runs along the rifle (0 = the plate's centre, + toward the
// muzzle), b across it (+ = the rifle's top: sights and comb; - = its belly: bayonet, stock toe).
const TH = 47 * Math.PI / 180;
const CX = 40, CY = 46;
const AX = Math.cos(TH), AY = -Math.sin(TH);        // along the rifle, toward the muzzle
const NX = -Math.sin(TH), NY = -Math.cos(TH);       // the rifle's top side (up-left)
const pt = (s, b) => (CX + s * AX + b * NX).toFixed(2) + "," + (CY + s * AY + b * NY).toFixed(2);
const shape = list => "M" + list.map(([s, b]) => pt(s, b)).join(" L") + " Z";

const HANDGUARD = shape([[14, 2.5], [36.6, 2.5], [37.6, 1.6], [37.6, -1.6], [36.6, -2.5], [14, -2.5]]);
const SIGHT = shape([[37, 1.3], [38.6, 4.6], [39.6, 4.6], [41.2, 1.3]]);
const BARREL = shape([[37.4, 1.15], [47.2, 1.15], [47.2, -1.15], [37.4, -1.15]]);
const HIDER = shape([[46.8, 1.6], [50.4, 1.6], [50.4, -1.6], [46.8, -1.6]]);
const BAYO_GRIP = shape([[38.6, -1.2], [46.6, -1.2], [46.6, -3.8], [39.4, -3.8]]);
// the blade: straight spine along the barrel, the edge sweeping up to the point (kept inside x 80)
const BAYO_BLADE = `M${pt(46.4, -1.2)} L${pt(53.4, -1.2)} L${pt(55.6, -1.5)} C${pt(53.8, -3.2)} ${pt(52, -4.3)} ${pt(49.6, -4.3)} L${pt(46.4, -4.3)} Z`;
const BAYO_FULLER = `M${pt(47.2, -2.5)} L${pt(51.6, -2.5)}`;
// the stock: a straight comb, a belly that swoops down from the wrist to the toe
const STOCK = `M${pt(-14, 2.2)} L${pt(-54, 2.2)} L${pt(-54, -7.2)} C${pt(-45, -4.8)} ${pt(-36, -2.6)} ${pt(-28, -2.6)} L${pt(-14, -2.6)} Z`;
const STOCK_SHADE = `M${pt(-51.8, -0.4)} L${pt(-30, -0.4)} L${pt(-30, -2.6)} C${pt(-37, -2.6)} ${pt(-45, -4.6)} ${pt(-51.8, -6.6)} Z`;
const BUTTPLATE = shape([[-54, 2.2], [-51.8, 2.2], [-51.8, -6.6], [-54, -7.2]]);

// An M1 steel pot from the front: a round dome whose sides drop straight to a thin, flat rim.
const DOME = "M32,8.4 C45.6,8.4 55.4,17 56.8,30 C57.1,33 57.2,36 57,39.4 L7,39.4 C6.8,36 6.9,33 7.2,30 C8.6,17 18.4,8.4 32,8.4 Z";
const RIM = "M5.6,38.4 L58.4,38.4 C59.8,38.4 60.8,39.4 60.8,40.6 C60.8,41.8 59.8,42.8 58.4,42.8 L5.6,42.8 C4.2,42.8 3.2,41.8 3.2,40.6 C3.2,39.4 4.2,38.4 5.6,38.4 Z";
const HELMET = DOME + " " + RIM;

module.exports = {
  rank: 3,
  name: "Soldier",
  plate: "oklch(.28 .045 148)",
  ink: "oklch(.775 .05 133)",
  glyphBox: [19.5, 22.5, 41, 41],
  glyph: `<symbol id="gl-3n" viewBox="0 0 64 64">`
    + `<path d="${HELMET}" fill="currentColor"></path>`
    // the dome turns away from the light on its right
    + `<path d="M38,6 C51,11 54,25 51.4,32 C50.4,35 50.4,39 50.8,44 L64,44 L64,6 Z" fill="#000" opacity=".28" mask="url(#r3-helm)"></path>`
    // the camouflage cover, blotched like the plate
    + `<path d="M4,14 C8,12 13,13.5 16,12 C19.5,10.4 24,11 25,13.6 C26,16.2 22.4,17 20.6,18.6 C18.8,20.2 19.6,22.4 16,22.8 C12,23.2 8,21 4,21 Z`
    + ` M33,9.6 C36.6,8.6 40,10.8 43.6,10.2 C47.2,9.6 50.4,12 50.8,14.8 C51.2,17.6 48,18.2 45.6,17.8 C43,17.4 41.4,19.6 38.4,19.4 C35.6,19.2 34.8,16.8 35.6,14.8 C36.2,13.2 32,12 33,9.6 Z`
    + ` M4,30 C8,29.4 11.6,31.6 15,31 C18.4,30.4 21.6,32 21.4,34.6 C21.2,37 18,37.6 16,39.4 L4,39.4 Z`
    + ` M27,39.4 C27.4,36.6 30,35.6 32.4,34.6 C35,33.6 36.6,31.4 39.6,31.8 C42.6,32.2 43.4,34.8 42,36.4 C40.8,37.8 42.6,39.4 42.6,39.4 Z`
    + ` M50,30.6 C53,30 56,30.8 60,31.2 L60,39.4 L52.6,39.4 C53.4,37.4 51.4,36.4 49.6,35 C48,33.8 47.8,31.2 50,30.6 Z"`
    + ` fill="#000" opacity=".18" mask="url(#r3-helm)"></path>`
    // cover band round the dome
    + `<path d="M0,20.4 C16,24.6 48,24.6 64,20.4 L64,27.4 C48,31.6 16,31.6 0,27.4 Z" fill="#000" opacity=".64" mask="url(#r3-helm)"></path>`
    // the seam where the dome meets the rim, and the rim's underside
    + `<path d="M7,39.1 L57,39.1" fill="none" stroke="#000" stroke-width="1.2" opacity=".45"></path>`
    + `<path d="M3.5,41.4 L60.5,41.4 C60.1,42.3 59.4,42.8 58.4,42.8 L5.6,42.8 C4.6,42.8 3.9,42.3 3.5,41.4 Z" fill="#000" opacity=".22"></path>`
    // chinstrap bales
    + `<circle cx="11" cy="35" r="1.6" fill="#000" opacity=".55"></circle><circle cx="53" cy="35" r="1.6" fill="#000" opacity=".55"></circle>`
    // chinstrap and buckle
    + `<path d="M11,42.4 C13.2,50.4 21.4,55 32,55 C42.6,55 50.8,50.4 53,42.4" fill="none" stroke="currentColor" stroke-width="2.6"></path>`
    + `<rect x="28.5" y="52.2" width="7" height="5.6" rx="1" fill="currentColor"></rect>`
    + `<rect x="30.2" y="53.8" width="3.6" height="2.4" fill="#000" opacity=".6"></rect>`
    + `</symbol>`,
  defs: `<mask id="r3-helm" maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64"><path d="${HELMET}" fill="#fff"></path></mask>`
    + `<mask id="r3-face" maskUnits="userSpaceOnUse" x="0" y="0" width="80" height="92"><polygon points="40,18 64,30.1 64,54.1 40,74 16,54.1 16,30.1" fill="#fff"></polygon></mask>`,
  plateMod(plate, ctx) {
    // woodland camouflage blotched into the face
    const camo = `<g mask="url(#r3-face)">`
      + `<g fill="#000" opacity=".17">`
      + `<path d="M14,33 C18,30 24,31 25.5,35 C27,39 22.5,41.5 19,43.5 C16.5,45 14,43 14,40 Z"></path>`
      + `<path d="M66,35 C61,34 56,37 56.5,41 C57,44.5 61,45.5 66,48 Z"></path>`
      + `<path d="M19,57 C22,52.5 28.5,53 30.5,57.5 C32.5,62 28.5,65.5 24.5,64 C21.5,63 17.5,60.5 19,57 Z"></path>`
      + `<path d="M44,63 C47,59.5 52.5,60 54.5,56.5 C56.5,53 61,53.5 62.5,56.5 C60,61 54,64.5 49,67 C45.5,68.5 42,66 44,63 Z"></path>`
      + `<path d="M35,19.5 C38.5,18.8 44,20.5 45,23.4 C42,24.8 37,24.4 35,22.2 Z"></path>`
      + `</g><g fill="${ctx.I}" opacity=".06">`
      + `<path d="M14,47 C17,45 21.5,46.5 22,49.5 C22.5,52 19.5,53 16.5,52.5 C15,52.2 14,51 14,49 Z"></path>`
      + `<path d="M59,44 C62,43 65,45 65,48 L65,52 C62.5,52.5 59,51.5 58,49 C57.2,47 57.5,44.6 59,44 Z"></path>`
      + `<path d="M33,66 C35.5,63.5 40,64 41,66.5 C42,69 39,71.5 36,70.5 C33.8,69.8 32,68 33,66 Z"></path>`
      + `<path d="M22,26 C24.5,24.5 28,25.5 28.2,27.6 C28.4,29.4 25.6,30.4 23.4,29.8 C21.6,29.2 20.8,26.8 22,26 Z"></path>`
      + `</g></g>`;
    const face = plate.indexOf(`<polygon points="40,18 64,30.1 64,54.1 40,74 16,54.1 16,30.1" fill="none"`);
    plate = plate.slice(0, face) + camo + plate.slice(face);
    // the rank's two machined dots move up into the shoulders, clear of the helmet
    // and the centre line goes: it showed through between the rim and the chinstrap like a nose
    return plate.replace(/<circle cx="24" cy="36"/, `<circle cx="22.4" cy="32"`)
                .replace(/<circle cx="56" cy="36"/, `<circle cx="57.6" cy="32"`)
                .replace(/<line x1="40" y1="18" x2="40" y2="74"[^>]*><\/line>/, "");
  },
  ornaments(ctx) {
    const { P, I, div } = ctx;
    const wood = `fill="${P}" stroke="${I}" stroke-width="1.3" stroke-linejoin="round"`;
    const steel = `fill="${I}" stroke="${I}" stroke-width="1" stroke-linejoin="round"`;
    const muzzle = ctx.both(HANDGUARD, wood) + ctx.both(SIGHT, steel) + ctx.both(BARREL, steel) + ctx.both(HIDER, steel);
    if (div === 2) return { behind: muzzle };
    if (div === 3) return {
      behind: ctx.both(STOCK, wood) + ctx.both(STOCK_SHADE, `fill="#000" opacity=".28"`) + ctx.both(BUTTPLATE, steel)
        + ctx.both(BAYO_GRIP, wood) + ctx.both(BAYO_BLADE, steel) + ctx.both(BAYO_FULLER, `fill="none" stroke="#000" stroke-width=".7" opacity=".35"`) + muzzle,
    };
    return {};
  },
};
