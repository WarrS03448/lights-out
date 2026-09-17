'use strict';
/**
 * The slur filter, service-side. A PORT of hub/censor.py, and it has to stay one.
 *
 * WHY THERE ARE TWO COPIES. The hub filters what you type so the word never reaches your own
 * screen. That was enough while chat was a local echo; it stopped being enough the moment a line
 * reaches nine other people, because the hub runs on the sender's machine and a patched client
 * simply does not call it. This copy runs before the broadcast, which is the only place an
 * attacker cannot reach.
 *
 * WHY IT IS NOT SHARED CODE. The hub ships frozen by PyInstaller and the service ships as the
 * `server/` folder alone; there is no runtime either of them can both import. So it is duplicated,
 * and the duplication is held together by two things rather than by hope:
 *
 *   1. DATA below is a verbatim copy of hub/censor.py's tables, and
 *      tests/test_censor.py::test_the_server_copy_carries_the_same_data reads THIS FILE, parses
 *      DATA out of it and compares every field. Drift in the blocklist, the lookalikes, the leet
 *      map, the suffixes, the protected words or either threshold fails the Python suite.
 *   2. The matcher below is a line-for-line port, and server/scripts/test-censor.mjs asserts the
 *      same properties the Python suite does - every entry masked, every bypass masked, every
 *      ordinary word untouched - generating its cases from DATA the same way.
 *
 * Read hub/censor.py for WHY the matcher is shaped like this: the folding, the squashed stream,
 * the per-letter repetition, the elision variant that catches `n*gger`, the boundary anchoring
 * that keeps `Scunthorpe`, and _ORDINARY as the release valve. None of that reasoning is repeated
 * here, because a second copy of an explanation is a second thing to get out of date.
 *
 * One deliberate difference: the fold uses toLowerCase() where Python uses casefold(). They agree
 * on everything that reaches it, because the one character they disagree about (ß) is in the
 * lookalike table on both sides rather than left to the language.
 */

// ---------------------------------------------------------------- DATA (generated; see above)
const DATA = {"MASK": "*", "SEP": "\u0001", "MIN_ELIDE": 5, "MIN_LOOSE_TAIL": 5, "CONFUSABLES": {"\u0430": "a", "\u0432": "b", "\u0441": "c", "\u0435": "e", "\u0440": "p", "\u043e": "o", "\u0445": "x", "\u0443": "y", "\u043d": "h", "\u043a": "k", "\u043c": "m", "\u0442": "t", "\u0456": "i", "\u0458": "j", "\u0455": "s", "\u04bb": "h", "\u0501": "d", "\u051b": "q", "\u03b1": "a", "\u03b5": "e", "\u03b9": "i", "\u03ba": "k", "\u03bd": "v", "\u03bf": "o", "\u03c1": "p", "\u03c4": "t", "\u03c5": "u", "\u03c7": "x", "\u03b3": "y", "\u03b7": "n", "\u00f8": "o", "\u0111": "d", "\u0142": "l", "\u0127": "h", "\u0167": "t", "\u20ac": "e", "\u0261": "g", "\u0262": "g", "\u0280": "r", "\u1d0f": "o", "\u026a": "i", "\u00df": "ss"}, "LEET": {"4": "a", "@": "a", "8": "b", "(": "c", "<": "c", "{": "c", "3": "e", "6": "g", "9": "g", "#": "h", "1": "i", "!": "i", "|": "i", "0": "o", "5": "s", "$": "s", "7": "t", "+": "t"}, "SUFFIXES": ["s", "es", "z", "ez", "er", "ers", "ed", "ing", "in", "a", "as", "az"], "ORDINARY": ["beaneries", "beanery", "chinkapin", "chinkapins", "homogeneous", "homogenise", "homogenize", "homogenous", "homograph", "homonym", "homophobe", "homophobes", "homophobia", "homophobic", "homosexual", "homosexuality", "homosexually", "homosexuals", "jape", "japed", "japer", "japers", "japes", "japing", "negroni", "negronis", "niggard", "niggardliness", "niggardly", "niggards", "pakistan", "pakistani", "pakistanis", "spice", "spiced", "spicer", "spicers", "spices", "spicey", "spicing", "squawk", "squawked", "squawker", "squawkers", "squawking", "squawks", "trans", "transfeminine", "transgender", "transgendered", "transition", "transitioned", "transitioning", "transitions", "transmasculine", "transphobe", "transphobes", "transphobia", "transphobic", "transsexual", "transsexuals"], "BLOCKLIST": "cnxuaWdnZXIKcnxuaWdnZXJzCnJ8bmlnZ2EKcnxuaWdnYXMKcnxuaWdnYWgKcnxuaWdnYWhzCnJ8bmlnZ2F6CnJ8bmlnZ2FyCnJ8bmlnbGV0CnJ8bmlnbGV0cwpyfG5lZ3JvCnJ8bmVncm9zCnJ8bmVncm9lcwpyfG5lZ3JvaWQKcnxuZWdyb2lkcwpyfGNvb24Kcnxjb29ucwpyfGppZ2Fib28KcnxqaWdhYm9vcwpyfGppZ2dhYm9vCnJ8amlnZ2Fib29zCnJ8cG9yY2htb25rZXkKcnxwb3JjaG1vbmtleXMKcnxzcGVhcmNodWNrZXIKcnxzcGVhcmNodWNrZXJzCnJ8dGFyYmFieQpyfGp1bmdsZWJ1bm55CnJ8anVuZ2xlYnVubmllcwpyfHBpY2thbmlubnkKcnxwaWNrYW5pbm5pZXMKcnxnb2xseXdvZwpyfGdvbGxpd29nCnJ8Z29sbHl3b2dzCnJ8Z29sbGl3b2dzCnJ8a2lrZQpyfGtpa2VzCnJ8eWlkCnJ8eWlkcwpyfGNoaW5rCnJ8Y2hpbmtzCnJ8Y2hpbmt5CnJ8Z29vawpyfGdvb2tzCnJ8c2xhbnRleWUKcnxzbGFudGV5ZXMKcnxzbG9wZWhlYWQKcnxzbG9wZWhlYWRzCnJ8emlwcGVyaGVhZApyfHppcHBlcmhlYWRzCnJ8amFwCnJ8amFwcwpyfHBha2kKcnxwYWtpcwpyfHJhZ2hlYWQKcnxyYWdoZWFkcwpyfHRvd2VsaGVhZApyfHRvd2VsaGVhZHMKcnxzYW5kbmlnZ2VyCnJ8c2FuZG5pZ2dlcnMKcnxjYW1lbGpvY2tleQpyfHdldGJhY2sKcnx3ZXRiYWNrcwpyfHNwaWMKcnxzcGljcwpyfHNwaWNrCnJ8c3BpY2tzCnJ8YmVhbmVyCnJ8YmVhbmVycwpyfGdyZWFzZWJhbGwKcnxncmVhc2ViYWxscwpyfHdvcApyfHdvcHMKcnxkYWdvCnJ8ZGFnb3MKcnxkYWdvZXMKcnxwb2xhY2sKcnxwb2xhY2tzCnJ8aG9ua3kKcnxob25rZXkKcnxob25raWVzCnJ8YWJvCnJ8YWJvcwpyfGJvb25nCnJ8Ym9vbmdzCnJ8Y29vbGllCnJ8Y29vbGllcwpyfGhhbGZicmVlZApyfGhhbGZicmVlZHMKcnxtdWxhdHRvCnJ8bXVsYXR0b3MKcnxtdWxhdHRvZXMKcnxvY3Rvcm9vbgpyfHF1YWRyb29uCnJ8cmVkc2tpbgpyfHJlZHNraW5zCnJ8c3F1YXcKcnxzcXVhd3MKcnxpbmp1bgpyfGluanVucwpyfHdpZ2dlcgpyfHdpZ2dlcnMKaHxmYWdnb3QKaHxmYWdnb3RzCmh8ZmFnb3QKaHxmYWdvdHMKaHxmYWdneQpofGZhZ2dvdHJ5Cmh8ZmFnCmh8ZmFncwpofGR5a2UKaHxkeWtlcwpofGJ1bGxkeWtlCmh8YnVsbGR5a2VzCmh8YnVsbGRhZ2dlcgpofGJ1bGxkYWdnZXJzCmh8aG9tbwpofGhvbW9zCmh8YmF0dHlib3kKaHxiYXR0eWJveXMKaHxiYXR0eW1hbgpofGJhdHR5bWVuCmh8cG9vZnRlcgpofHBvb2Z0ZXJzCmh8Y2FycGV0bXVuY2hlcgpofGNhcnBldG11bmNoZXJzCmh8cnVnbXVuY2hlcgpofHJ1Z211bmNoZXJzCmh8ZnVkZ2VwYWNrZXIKaHxmdWRnZXBhY2tlcnMKdHx0cmFubnkKdHx0cmFubmllCnR8dHJhbm5pZXMKdHx0cmFubnlzCnR8c2hlbWFsZQp0fHNoZW1hbGVzCnR8bGFkeWJveQp0fGxhZHlib3lzCnR8dHJvb24KdHx0cm9vbnMKdHxkaWNrZ2lybAp0fGRpY2tnaXJscwp0fHRyYW5zdHJlbmRlcgp0fHRyYW5zdHJlbmRlcnM="};

const MASK = DATA.MASK;
const SEP = DATA.SEP;
const ORDINARY = new Set(DATA.ORDINARY);

/** [[category, word]] - "r" racist, "h" homophobic, "t" transphobic. */
function entries() {
  const text = Buffer.from(DATA.BLOCKLIST, 'base64').toString('utf8');
  const out = [];
  for (const line of text.split('\n')) {
    const cut = line.indexOf('|');
    if (cut > 0 && cut < line.length - 1) out.push([line.slice(0, cut), line.slice(cut + 1)]);
  }
  return out;
}

function words(category) {
  return entries().filter(([c]) => !category || c === category).map(([, w]) => w);
}

// ---------------------------------------------------------------- normalising
const COMBINING = /\p{M}/gu;

/** One character of input as zero or more plain ascii characters. */
function fold(ch) {
  const known = DATA.CONFUSABLES[ch];
  if (known !== undefined) return known;
  return ch.normalize('NFKD').replace(COMBINING, '').toLowerCase();
}

/**
 * {stream, spans} - `text` as ascii letters with one SEP per run of anything else.
 *
 * spans[i] is the [start, end) slice of the ORIGINAL string that stream character i came from.
 * Iteration is by CODE POINT (so an astral lookalike folds as one character) while the spans stay
 * in UTF-16 units, because that is what slicing and masking the original string needs.
 */
function squash(text, leetAsLetters) {
  const stream = [];
  const spans = [];
  let gapStart = null;
  let i = 0;
  for (const ch of text) {
    const width = ch.length;
    for (const folded of fold(ch)) {
      let letter = '';
      if (folded >= 'a' && folded <= 'z') letter = folded;
      else if (leetAsLetters && DATA.LEET[folded] !== undefined) letter = DATA.LEET[folded];
      if (letter) {
        if (gapStart !== null) { stream.push(SEP); spans.push([gapStart, i]); gapStart = null; }
        stream.push(letter);
        spans.push([i, i + width]);
      } else if (gapStart === null) {
        gapStart = i;
      }
    }
    i += width;
  }
  // a trailing run of separators is dropped: nothing can match past the last letter
  return { stream: stream.join(''), spans };
}

// ---------------------------------------------------------------- the patterns
function esc(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** The regex branches for one blocked word: the plain one, then one per INTERIOR letter with that
 *  letter replaced by a required separator. */
function variants(word) {
  const letters = Array.from(word, (c) => esc(c) + '+');
  const gap = esc(SEP) + '?';
  const out = [letters.join(gap)];
  if (word.length >= DATA.MIN_ELIDE) {
    for (let k = 1; k < word.length - 1; k++) {
      const pieces = [];
      for (let n = 0; n < letters.length; n++) {
        if (n === k) pieces.push(esc(SEP));
        else if (n && n - 1 !== k) pieces.push(gap + letters[n]);
        else pieces.push(letters[n]);
      }
      out.push(pieces.join(''));
    }
  }
  return out;
}

const SUFFIX_ALT = '(?:' + DATA.SUFFIXES.slice().sort((a, b) => b.length - a.length).join('|') + ')?';
const END = '(?=' + esc(SEP) + '|$)';

/** The shortest continuations that turn this entry into an ordinary word. */
function collides(word) {
  const tails = DATA.ORDINARY
    .filter((w) => w !== word && w.startsWith(word))
    .map((w) => w.slice(word.length))
    .sort();
  const shortest = [];
  for (const tail of tails) {
    if (!shortest.some((seen) => tail.startsWith(seen))) shortest.push(tail);
  }
  return shortest;
}

function branch(word) {
  let tail;
  if (word.length < DATA.MIN_LOOSE_TAIL) {
    tail = SUFFIX_ALT;
  } else {
    const guard = collides(word);
    tail = (guard.length ? '(?!' + guard.join('|') + ')' : '') + '[a-z]*';
  }
  return '(?:' + variants(word).join('|') + ')' + tail + END;
}

/** {first letter: RegExp}, longest word first inside each - see hub/censor.py _build. */
let PATTERNS = null;

function patterns() {
  if (PATTERNS === null) {
    const byLetter = new Map();
    const sorted = entries().slice().sort((a, b) => (b[1].length - a[1].length) || a[1].localeCompare(b[1]));
    for (const [, word] of sorted) {
      if (!byLetter.has(word[0])) byLetter.set(word[0], []);
      byLetter.get(word[0]).push(branch(word));
    }
    PATTERNS = new Map();
    for (const [letter, branches] of byLetter) {
      // sticky, so .lastIndex anchors the test at exactly the candidate position
      PATTERNS.set(letter, new RegExp(branches.join('|'), 'y'));
    }
  }
  return PATTERNS;
}

// ---------------------------------------------------------------- matching
function spansOf(text, leetAsLetters) {
  const { stream, spans } = squash(text, leetAsLetters);
  const found = [];
  const n = stream.length;
  let i = 0;
  while (i < n) {
    if (stream[i] !== SEP && (i === 0 || stream[i - 1] === SEP)) {
      const rx = patterns().get(stream[i]);
      if (rx !== undefined) {
        rx.lastIndex = i;
        const m = rx.exec(stream);
        if (m !== null) {
          const end = i + m[0].length;
          // The pattern ends at a boundary, so this IS the whole word the match sits in.
          if (!ORDINARY.has(stream.slice(i, end))) found.push([spans[i][0], spans[end - 1][1]]);
          i = end;
          continue;
        }
      }
    }
    i += 1;
  }
  return found;
}

/** `text` with every blocked word replaced by MASK, one character for each covered. */
function censor(text) {
  if (!text) return text;
  const hits = spansOf(text, true).concat(spansOf(text, false));
  if (!hits.length) return text;
  const out = Array.from(text);
  // Array.from splits by code point, so the UTF-16 spans need mapping onto it first.
  const at = [];
  let cursor = 0;
  for (const ch of out) { at.push(cursor); cursor += ch.length; }
  for (const [start, end] of hits) {
    for (let k = 0; k < out.length; k++) {
      if (at[k] >= start && at[k] < end) out[k] = MASK;
    }
  }
  return out.join('');
}

function isClean(text) {
  return !spansOf(text, true).length && !spansOf(text, false).length;
}

module.exports = { censor, isClean, entries, words, DATA };
