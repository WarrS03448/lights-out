/**
 * THE LADDER. One file, because there is one ladder and two modules that need it.
 *
 * `progress.cjs` owns the VISIBLE rank - what a player sees, climbed in RR. `rating.cjs` owns the
 * HIDDEN MMR - what the matchmaker reads, and it needs to know how many rungs the ladder has so
 * it can say which one an MMR deserves. Neither can require the other (progress already requires
 * rating), and both of them keeping their own copy is exactly how this repository ended up with
 * two ladders: `Static, Witness, Responder, Operator, Enforcer, Nightwatch, Ghostframe, Blackout`
 * in one and `Rookie .. Reaper` in the other, and a hub that drew a badge from one beside a
 * tier label from the other.
 *
 * So the names live here and nowhere else. See docs/ranks.md, which this file is the code of.
 *
 * A CAREER, THEN A HUNT (Sam, 2026-09-16). The shape is Valorant's and deliberately so - he asked
 * for "that same level of 'getting cooler'" - which is an arc, not a theme.
 *
 *   Rookie, Private,           four plain career rungs. NO INSIGNIA, on purpose: an earlier
 *   Soldier, Veteran           draft ran Private/Corporal/Sergeant/Lieutenant/Captain and Sam
 *                              killed it, because those only sort themselves for somebody who
 *                              already knows the insignia. These four sort themselves for
 *                              anybody - a Veteran beats a Soldier beats a Rookie, in English.
 *   Operator, Shadow,          the turn. It happens ONCE, at rank 5, and after it the ladder
 *   Nightmare, Spectre         stops describing a job and starts describing what you are:
 *                              selected, then unseen, then feared, then not there at all.
 *   Reaper                     the capstone, one band, no divisions. Death is the only thing
 *                              left above a ghost, which is why it ends here and not at Spectre.
 *
 * THE RULE THIS LIST IS BUILT ON: a player must be able to order any two of these names having
 * never read a word of documentation. That is what killed the insignia draft and the metals draft
 * before it, and it is the thing to check first if anyone proposes a new name.
 *
 * The top five leave the chain of command because a promotion ladder has nowhere good to end -
 * Colonel and General are desks, and this is a shooter. They are also how the ladder keeps
 * climbing INTO the dark rather than out of it, which belongs to this game rather than to the
 * genre: Bodycam is night raids shot on a bodycam, a ladder ending in Radiant would be borrowing
 * someone else's mood, and the product is called Lights Out.
 *
 * `Operator` was also in the placeholder set deleted on 2026-09-15 (Static/Witness/Responder/
 * Operator/Enforcer/Nightwatch/Ghostframe/Blackout). That set died for being a SECOND ladder drawn
 * beside this one, not for its vocabulary, so reusing the word is deliberate and safe. Note that
 * `Blackout` in the history is usually THAT list's rank 8 - it was also this ladder's capstone for
 * part of 2026-09-16, and was cut. See docs/ranks.md.
 *
 * Every name is an environment dial, so a deploy can rename a rank without a release. Nothing
 * anywhere keys off a rank's NAME - the hub renders whatever `hello` sent it and the badge art is
 * keyed by INDEX (docs/rank-art.md) - which is what makes that safe.
 */

/** The ranks that have divisions, in order, bottom first. */
const NAMES = (process.env.COMP_RANK_NAMES
  || 'Rookie,Private,Soldier,Veteran,Operator,Shadow,Nightmare,Spectre')
  .split(',').map((s) => s.trim()).filter(Boolean);

/** The capstone above them: one band, no divisions, and the figure keeps counting. '' removes it. */
const TOP = (process.env.COMP_RANK_TOP === undefined
  ? 'Reaper' : process.env.COMP_RANK_TOP).trim();

// COMP_RANK_DIVISIONS is accepted as well because rating.cjs used to read the ladder under that
// name; a service already setting it keeps working.
const DIVISIONS = Math.max(1, Number(process.env.COMP_DIVISIONS)
  || Number(process.env.COMP_RANK_DIVISIONS) || 3);

/** How many ranks have divisions. Every named one - the capstone is not in NAMES. */
const TIERED = Math.max(1, NAMES.length);

/** How many RUNGS the ladder has, capstone included. This is the 1..N scale ranks are numbered on. */
const RANKS = TIERED + (TOP ? 1 : 0);

/** How many divisions there are below the capstone - 24 by default, and 25 rungs with it. */
const BANDS = TIERED * DIVISIONS;

/** The name of a 1-based rank, capstone included. '' for a rank off the end of the ladder. */
function nameOf(rank) {
  const n = Math.floor(Number(rank) || 0);
  if (TOP && n >= RANKS) return TOP;
  return NAMES[Math.max(1, Math.min(TIERED, n)) - 1] || '';
}

module.exports = { NAMES, TOP, DIVISIONS, TIERED, RANKS, BANDS, nameOf };
