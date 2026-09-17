#!/usr/bin/env node
// The service-side slur filter: server/censor.cjs.
//
// NO SLUR IS SPELLED OUT HERE. Every case is generated from the module's own blocklist, which is
// what it has to catch anyway. The words that must NOT be caught are ordinary English and are
// written in full, because that half matters just as much - a filter that eats `Scunthorpe` or
// `homosexual` gets switched off, and then it catches nothing.
//
// This mirrors tests/test_censor.py property for property. The two files are the reason the port
// can be trusted: the Python suite checks that server/censor.cjs carries the same DATA, and this
// one checks that the same DATA behaves the same way once it is matched against.

import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const require_ = createRequire(import.meta.url);
const censor = require_('../censor.cjs');

const { DATA } = censor;
const WORDS = censor.words();
const LONG = WORDS.filter((w) => w.length >= DATA.MIN_ELIDE);
const CATEGORIES = ['r', 'h', 't'];

const LEET = new Map();
for (const [sym, letter] of Object.entries(DATA.LEET)) if (!LEET.has(letter)) LEET.set(letter, sym);
const CONFUSABLE = new Map();
for (const [ch, letter] of Object.entries(DATA.CONFUSABLES)) {
  if (letter.length === 1 && !CONFUSABLE.has(letter)) CONFUSABLE.set(letter, ch);
}

const results = [];
function test(name, fn) {
  try { fn(); results.push([name, null]); } catch (e) { results.push([name, e]); }
}

const masked = (text) => {
  const out = censor.censor(text);
  return out !== text && Array.from(out).every((c) => c === DATA.MASK);
};
const maskedRun = (out, least) => out.includes(DATA.MASK.repeat(least));

// ---------------------------------------------------------------- the list
test('the blocklist decodes and every entry is categorised', () => {
  assert.ok(WORDS.length > 100);
  assert.equal(WORDS.length, new Set(WORDS).size, 'duplicate entry');
  assert.ok(WORDS.every((w) => /^[a-z]+$/.test(w)), 'entries are plain lowercase letters');
  let total = 0;
  for (const cat of CATEGORIES) {
    assert.ok(censor.words(cat).length >= 10, `category ${cat} is thin`);
    total += censor.words(cat).length;
  }
  assert.equal(total, WORDS.length, 'an entry with no category');
});

test('every blocked word is caught, alone and in a sentence', () => {
  for (const w of WORDS) {
    assert.ok(masked(w), w);
    const out = censor.censor(`you are such a ${w} mate`);
    assert.ok(out.includes(DATA.MASK.repeat(w.length)), w);
    assert.ok(out.startsWith('you are such a ') && out.endsWith(' mate'), w);
  }
});

// ---------------------------------------------------------------- the bypasses
test('case does not help', () => {
  for (const w of WORDS) {
    assert.ok(masked(w.toUpperCase()), w);
    assert.ok(masked(w[0].toUpperCase() + w.slice(1)), w);
  }
});

test('spacing and punctuation between letters do not help', () => {
  for (const w of WORDS) {
    for (const glue of [' ', '.', '-', '_', '*', ' . ', '​']) {
      assert.ok(masked(Array.from(w).join(glue)), `${w} / ${JSON.stringify(glue)}`);
    }
  }
});

test('repeated letters do not help', () => {
  for (const w of WORDS) assert.ok(masked(Array.from(w, (c) => c.repeat(3)).join('')), w);
});

test('leetspeak does not help', () => {
  for (const w of WORDS) {
    const leet = Array.from(w, (c) => LEET.get(c) || c).join('');
    if (leet !== w) assert.ok(masked(leet), w);
  }
});

test('unicode lookalikes and fullwidth do not help', () => {
  for (const w of WORDS) {
    const cyrillic = Array.from(w, (c) => CONFUSABLE.get(c) || c).join('');
    if (cyrillic !== w) assert.ok(masked(cyrillic), w);
    const fullwidth = Array.from(w, (c) => String.fromCodePoint(c.codePointAt(0) - 97 + 0xff41)).join('');
    assert.ok(masked(fullwidth), w);
  }
});

test('a symbol standing in for a letter does not help', () => {
  for (const w of LONG) {
    for (let k = 1; k < w.length - 1; k++) {
      for (const sym of ['*', '#', '%']) {
        const bypass = w.slice(0, k) + sym + w.slice(k + 1);
        assert.notEqual(censor.censor(bypass), bypass, `${w} ${k} ${sym}`);
      }
    }
  }
});

test('digits and symbols on the end do not help', () => {
  for (const w of WORDS) {
    for (const tail of ['1', '69', '!!!', '_', '~']) {
      assert.notEqual(censor.censor(w + tail), w + tail, `${w}${tail}`);
    }
  }
});

test('letters on the end of a long entry do not help', () => {
  for (const w of WORDS) {
    if (w.length < DATA.MIN_LOOSE_TAIL) continue;
    for (const tail of ['xd', 'lol', '123x']) {
      assert.notEqual(censor.censor(w + tail), w + tail, `${w}${tail}`);
    }
  }
});

test('plurals and ordinary endings are caught', () => {
  for (const w of WORDS) {
    for (const suffix of ['s', 'es', 'ing', 'er']) {
      if (DATA.ORDINARY.includes(w + suffix)) continue;
      assert.ok(masked(w + suffix), `${w}+${suffix}`);
    }
  }
});

// ---------------------------------------------------------------- what must survive
const INNOCENT = [
  'Scunthorpe', 'assassin', 'assassinate', 'classic', 'cockpit', 'analysis', 'Matsushita',
  'shiitake', 'therapist', 'skyscraper',
  'raccoon', 'cocoon', 'tycoon', 'raccoons', 'cocoons',
  'snigger', 'sniggering', 'niggardly', 'bigger', 'digger', 'trigger',
  'Nigeria', 'Nigerian', 'negroni', 'negronis',
  'Japan', 'Japanese', 'jape', 'japes',
  'about', 'above', 'aboard', 'abort', 'abolish', 'abomination',
  'squawk', 'squawked', 'squawking', 'squawker',
  'spice', 'spices', 'spiced', 'spicy', 'spicier', 'spicing',
  'Pakistan', 'Pakistani', 'Pakistanis',
  'honking', 'honked', 'wiggle', 'wiggled', 'wiggler',
  'cooling', 'coolant', 'boondocks', 'injury', 'injured',
  'yield', 'yielded', 'flag', 'flags', 'flagging', 'flagship',
  'homosexual', 'homophobia', 'homophobic', 'homogeneous', 'homework', 'homer',
  'chinkapin', 'beanery', 'beans', 'greasy', 'greased',
  'dagger', 'daggers', 'polar', 'honest', 'colander',
  'niggard', 'niggards', 'niggardliness', 'homosexuals', 'homosexuality', 'homonym',
  'troop', 'troops', 'trooper', 'troopers',
];

test('ordinary words are left alone', () => {
  const eaten = INNOCENT.filter((w) => censor.censor(w) !== w);
  assert.deepEqual(eaten, [], `the filter ate ordinary words: ${eaten}`);
});

test('ordinary sentences are left alone', () => {
  for (const line of [
    'gg wp, close one',
    'ban Rome, we always lose there',
    'I am going to Nigeria in the spring and my flight lands in Japan',
    'the assassin in the cockpit had a classic analysis of the spices',
    'he was sniggering about the raccoon in Scunthorpe',
    '', '   ', '??? !!! ...',
  ]) assert.equal(censor.censor(line), line, line);
});

test('the words people use about themselves are never masked', () => {
  const SELF = ['trans', 'transgender', 'transgendered', 'transsexual', 'transitioning',
                'transition', 'transphobia', 'transphobic', 'transfeminine', 'transmasculine',
                'homosexual', 'homosexuality', 'homophobia', 'gay', 'gays', 'lesbian',
                'bisexual', 'nonbinary', 'enby', 'femboy', 'drag', 'pride'];
  const eaten = SELF.filter((w) => censor.censor(w) !== w);
  assert.deepEqual(eaten, [], `masked a self-description: ${eaten}`);
  for (const line of ['I am trans btw', 'she is transitioning', 'gay guy here, gg wp']) {
    assert.equal(censor.censor(line), line, line);
  }
});

test('the words the game uses are never masked', () => {
  for (const line of ['its a trap', 'trap the door', 'watch the traps on B', 'trapped']) {
    assert.equal(censor.censor(line), line, line);
  }
});

test('every protected word survives, with the junk-tail rule still on', () => {
  const eaten = DATA.ORDINARY.filter((w) => censor.censor(w) !== w);
  assert.deepEqual(eaten, [], `ate a protected word: ${eaten}`);
  for (const w of WORDS) {
    if (w.length < DATA.MIN_LOOSE_TAIL) continue;
    assert.notEqual(censor.censor(w + 'xd'), w + 'xd', w);
  }
});

test('unlisted inflections of a protected word are safe too', () => {
  for (const w of ['niggardliest', 'niggardliness', 'homosexualism', 'homosexualities',
                   'pakistanian', 'squawkiest', 'negronied']) {
    assert.equal(censor.censor(w), w, w);
  }
});

// ---------------------------------------------------------------- shape
test('the mask covers the word and never changes the length', () => {
  for (const w of WORDS) {
    const line = `x ${w} y`;
    const out = censor.censor(line);
    assert.equal(out.length, line.length, w);
    assert.ok(out.startsWith('x '), w);
    assert.equal(out.slice(2, 2 + w.length), DATA.MASK.repeat(w.length), w);
  }
});

test('isClean agrees with censor', () => {
  for (const w of WORDS) assert.equal(censor.isClean(w), false, w);
  for (const w of INNOCENT) assert.equal(censor.isClean(w), true, w);
});

test('a long line is filtered quickly', () => {
  const line = 'gg wp everyone that was a close one, ban Rome next time '.repeat(4).slice(0, 200);
  const started = process.hrtime.bigint();
  for (let i = 0; i < 50; i++) censor.censor(line);
  const ms = Number(process.hrtime.bigint() - started) / 1e6 / 50;
  assert.ok(ms < 20, `${ms.toFixed(2)} ms per 200-character line`);
});

// ---------------------------------------------------------------- report
let failed = 0;
for (const [name, err] of results) {
  if (err) { failed++; console.log(`FAIL  ${name}\n      ${err.message.split('\n')[0]}`); }
  else console.log(`ok    ${name}`);
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
