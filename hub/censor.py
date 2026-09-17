"""Mask racist, homophobic and transphobic slurs in player-written text, including attempts to
slip one past the filter (Sam, 2026-09-16: "all racist and homophobic words are censored as well as
attempts to bypass the censoring", then "make transphobic slurs be censored too").

WHAT IT IS FOR, AND WHAT IT IS NOT. This runs in the hub, on the machine of the person who typed
the message, so a modified client can always defeat it. That is fine for what it is: it takes the
slur off the screen and shows the sender that it was seen. It is NOT enforcement - reporting
(server/live.cjs REPORT_REASONS) is. When chat gains a real relay, the SERVICE has to run this same
rule before it broadcasts, because that is the only copy of it an attacker cannot patch out.

THE HARD PART IS NOT THE LIST, IT IS EVERYTHING AROUND IT. `censor("nigger")` is trivial; the work
is catching `N I G G E R`, `n1gg3r`, `niiiggerrr`, `n*gger`, `ｎｉｇｇｅｒ` and `nіggеr` (that one
is a Cyrillic і and е) while leaving `Scunthorpe`, `assassin`, `Nigeria`, `Japan`, `about`,
`cocoon`, `squawk` and `spices` alone. Both halves matter: a filter that misses the evasions is
decoration, and one that mangles ordinary words gets switched off, after which it catches nothing.

How it works, in order:

1. FOLD. Unicode is normalised (NFKD), combining marks are dropped and the text is casefolded, so
   `é`, `ｅ` and `𝐞` are all `e`. NFKD does not touch Cyrillic or Greek lookalikes, so those have
   an explicit table (_CONFUSABLES).
2. SQUASH. What is left becomes a stream of ascii letters plus ONE separator marker per run of
   anything else, so `n i g g e r` and `n.i.g.g.e.r` reduce to the same thing. Every character in
   the stream carries the span of the original text it came from, which is what lets a match found
   in the stream be masked in the text the player actually typed.
3. MATCH. Each blocked word becomes `n+.?i+.?g+.?g+.?e+.?r+` - every letter may repeat
   (`niiigger`) and a separator may sit between any two (`n i g g e r`). Words of five letters or
   more also get one variant per INTERIOR letter with that letter replaced by a REQUIRED separator,
   which is what catches `n*gger` and `f@ggot` without inventing matches out of ordinary text.
4. ANCHOR. This is what keeps `Scunthorpe` working. A match is only tried at a word boundary, and
   the pattern itself ends `(?:s|es|ing|er|...)?(?=SEP|$)`, so it has to finish at one too - or at
   a plain suffix. `ass` inside `assassin` starts at a boundary but does not end at one, `spic`
   inside `spicier` likewise, and `coon` inside `raccoon` does not start at one. The few ordinary
   words that still read as a hit (`spices` really is `spic` + `es`) are listed in _ORDINARY.

   BOTH RULES LIVE IN THE REGEX, which is not a tidiness point. Checked afterwards instead, a
   rejected match takes its start position with it: in "n i g g a, rematch" a longer entry matched
   across the comma, failed the suffix test, and the shorter word that was really there never got
   tried at that position at all. Inside the pattern, the engine backtracks into the other branches
   by itself.
5. TWICE. The whole thing runs once with `4 @ 3 1 0 $` read as letters (`n1gg3r`) and once with
   them read as separators (`fag1`, `faggot69`), and the two sets of spans are merged.

Known limits, on purpose. A letter that is simply MISSING (`nigr`), or an extra one wedged into
the middle (`niXgger`), is not caught: both need fuzzy matching, and fuzzy matching is how a filter
starts eating real words. Junk stuck on the END (`niggerxd`) IS caught, but not for the shortest
entries, where an ordinary word can begin with one - see MIN_LOOSE_TAIL and _ORDINARY.

Known false positives, also on purpose: `dyke` and `chink` are ordinary words in other contexts (a
levee, a gap in a wall) and are blocked anyway, because in a competitive lobby they are not being
used that way. And a mask can run past the word it found - separators inside a match belong to it,
which is the only reason `n i g g e r` can be masked at all, so a following word is swallowed when
it completes another entry. Masking a character too many is the safe direction to be wrong in.

THE LIST IS BASE64, not because that hides it from an attacker - it is right there in the module -
but so that the repository, its diffs, its pull requests and its CI logs do not carry a readable
page of slurs. `python -m hub.censor --list` prints it; `--encode` reads one word per line on
stdin and prints a replacement blob; `--check` filters stdin line by line.
"""
import base64
import re
import sys
import unicodedata

MASK = "*"
SEP = "\x01"                 # one of these per run of non-letters in the squashed stream

# Lookalikes NFKD does not fold: Cyrillic, Greek, and the few Latin letters carrying a stroke
# rather than a combining mark. Everything else (fullwidth, circled, math alphanumerics, accents)
# comes out of the NFKD pass, which is what keeps this table short enough to read.
_CONFUSABLES = {
    "а": "a", "в": "b", "с": "c", "е": "e", "р": "p", "о": "o",
    "х": "x", "у": "y", "н": "h", "к": "k", "м": "m", "т": "t",
    "і": "i", "ј": "j", "ѕ": "s", "һ": "h", "ԁ": "d", "ԛ": "q",
    "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o",
    "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "γ": "y", "η": "n",
    "ø": "o", "đ": "d", "ł": "l", "ħ": "h", "ŧ": "t", "€": "e",
    "ɡ": "g", "ɢ": "g", "ʀ": "r", "ᴏ": "o", "ɪ": "i",
    # Python's casefold() turns this into "ss" and JavaScript's toLowerCase() does not, so it is
    # spelled out here instead - the table is the one thing server/censor.cjs shares verbatim,
    # and a fold that depends on the language would be exactly the kind of drift that is hard
    # to see and impossible to test for from one side.
    "ß": "ss",
}

# Digits and punctuation that stand in for letters. Read as letters on the first pass and as
# separators on the second, because both readings are evasions and neither alone catches the other.
_LEET = {
    "4": "a", "@": "a", "8": "b", "(": "c", "<": "c", "{": "c", "3": "e", "6": "g", "9": "g",
    "#": "h", "1": "i", "!": "i", "|": "i", "0": "o", "5": "s", "$": "s", "7": "t", "+": "t",
}

# Endings a blocked word may carry and still be the blocked word. Deliberately short: every entry
# here is a chance to eat an ordinary word, and the ones NOT here are doing real work - `-an` keeps
# `Japan`, `-ia`/`-ian` keep `Nigeria`, `-ut`/`-ve` keep `about` and `above`, `-k` keeps `squawk`,
# `-y` keeps `spicy`, `-stani` keeps `Pakistani`.
_SUFFIXES = ("s", "es", "z", "ez", "er", "ers", "ed", "ing", "in", "a", "as", "az")

# ORDINARY WORDS THAT READ AS A HIT. One list, doing two jobs, and it is the release valve for
# every rule above - when the filter eats a real word, the answer is a line here, not a looser rule.
#
#   1. An entry one of these BEGINS with has that continuation cut out of its loose tail (see
#      _branch and _collides). This is what stops `homos` from eating `homosexual` and `pakis`
#      from eating `Pakistan`, and it is why the loose tail can be turned on at five letters.
#   2. A token that IS one of these is never masked, which covers the ones the suffix rule reaches
#      instead of the tail: `spices` really is `spic` + `es`, `japes` really is `jap` + `es`.
#
# `homosexual`, `homophobic` and the `trans...` vocabulary are in here on purpose even though
# nothing on the blocklist currently collides with them. A filter that masks the words people use
# to describe themselves - in a lobby where somebody is being called names - is worse than no
# filter, so those do not get to depend on the blocklist happening to stay the shape it is today.
# Add an entry that collides with one of them later and this list quietly keeps them safe.
_ORDINARY = frozenset((
    "spice", "spices", "spiced", "spicer", "spicers", "spicing", "spicey",
    "jape", "japes", "japed", "japing", "japer", "japers",
    "niggard", "niggards", "niggardly", "niggardliness",
    "beanery", "beaneries",
    "squawk", "squawks", "squawked", "squawking", "squawker", "squawkers",
    "negroni", "negronis",
    "chinkapin", "chinkapins",
    "pakistan", "pakistani", "pakistanis",
    "homosexual", "homosexuals", "homosexuality", "homosexually",
    "homophobe", "homophobes", "homophobia", "homophobic",
    "homogeneous", "homogenous", "homogenise", "homogenize", "homograph", "homonym",
    "trans", "transgender", "transgendered", "transsexual", "transsexuals",
    "transition", "transitions", "transitioned", "transitioning",
    "transfeminine", "transmasculine", "transphobe", "transphobes", "transphobia", "transphobic",
))

# `r|word` racist, `h|word` homophobic, `t|word` transphobic - 143 entries (r=101 h=28 t=14). The categories are not used to
# switch anything on or off: everything here is masked. They are there so that `--list` says what
# the scope actually is, and so a later change can reason about one group without re-reading all of
# it. See the module docstring for why it is encoded, and `--list` / `--encode` to read and rebuild.
_BLOCKLIST = (
    "cnxuaWdnZXIKcnxuaWdnZXJzCnJ8bmlnZ2EKcnxuaWdnYXMKcnxuaWdnYWgKcnxuaWdnYWhzCnJ8bmlnZ2F6CnJ8bmln"
    "Z2FyCnJ8bmlnbGV0CnJ8bmlnbGV0cwpyfG5lZ3JvCnJ8bmVncm9zCnJ8bmVncm9lcwpyfG5lZ3JvaWQKcnxuZWdyb2lk"
    "cwpyfGNvb24Kcnxjb29ucwpyfGppZ2Fib28KcnxqaWdhYm9vcwpyfGppZ2dhYm9vCnJ8amlnZ2Fib29zCnJ8cG9yY2ht"
    "b25rZXkKcnxwb3JjaG1vbmtleXMKcnxzcGVhcmNodWNrZXIKcnxzcGVhcmNodWNrZXJzCnJ8dGFyYmFieQpyfGp1bmds"
    "ZWJ1bm55CnJ8anVuZ2xlYnVubmllcwpyfHBpY2thbmlubnkKcnxwaWNrYW5pbm5pZXMKcnxnb2xseXdvZwpyfGdvbGxp"
    "d29nCnJ8Z29sbHl3b2dzCnJ8Z29sbGl3b2dzCnJ8a2lrZQpyfGtpa2VzCnJ8eWlkCnJ8eWlkcwpyfGNoaW5rCnJ8Y2hp"
    "bmtzCnJ8Y2hpbmt5CnJ8Z29vawpyfGdvb2tzCnJ8c2xhbnRleWUKcnxzbGFudGV5ZXMKcnxzbG9wZWhlYWQKcnxzbG9w"
    "ZWhlYWRzCnJ8emlwcGVyaGVhZApyfHppcHBlcmhlYWRzCnJ8amFwCnJ8amFwcwpyfHBha2kKcnxwYWtpcwpyfHJhZ2hl"
    "YWQKcnxyYWdoZWFkcwpyfHRvd2VsaGVhZApyfHRvd2VsaGVhZHMKcnxzYW5kbmlnZ2VyCnJ8c2FuZG5pZ2dlcnMKcnxj"
    "YW1lbGpvY2tleQpyfHdldGJhY2sKcnx3ZXRiYWNrcwpyfHNwaWMKcnxzcGljcwpyfHNwaWNrCnJ8c3BpY2tzCnJ8YmVh"
    "bmVyCnJ8YmVhbmVycwpyfGdyZWFzZWJhbGwKcnxncmVhc2ViYWxscwpyfHdvcApyfHdvcHMKcnxkYWdvCnJ8ZGFnb3MK"
    "cnxkYWdvZXMKcnxwb2xhY2sKcnxwb2xhY2tzCnJ8aG9ua3kKcnxob25rZXkKcnxob25raWVzCnJ8YWJvCnJ8YWJvcwpy"
    "fGJvb25nCnJ8Ym9vbmdzCnJ8Y29vbGllCnJ8Y29vbGllcwpyfGhhbGZicmVlZApyfGhhbGZicmVlZHMKcnxtdWxhdHRv"
    "CnJ8bXVsYXR0b3MKcnxtdWxhdHRvZXMKcnxvY3Rvcm9vbgpyfHF1YWRyb29uCnJ8cmVkc2tpbgpyfHJlZHNraW5zCnJ8"
    "c3F1YXcKcnxzcXVhd3MKcnxpbmp1bgpyfGluanVucwpyfHdpZ2dlcgpyfHdpZ2dlcnMKaHxmYWdnb3QKaHxmYWdnb3Rz"
    "Cmh8ZmFnb3QKaHxmYWdvdHMKaHxmYWdneQpofGZhZ2dvdHJ5Cmh8ZmFnCmh8ZmFncwpofGR5a2UKaHxkeWtlcwpofGJ1"
    "bGxkeWtlCmh8YnVsbGR5a2VzCmh8YnVsbGRhZ2dlcgpofGJ1bGxkYWdnZXJzCmh8aG9tbwpofGhvbW9zCmh8YmF0dHli"
    "b3kKaHxiYXR0eWJveXMKaHxiYXR0eW1hbgpofGJhdHR5bWVuCmh8cG9vZnRlcgpofHBvb2Z0ZXJzCmh8Y2FycGV0bXVu"
    "Y2hlcgpofGNhcnBldG11bmNoZXJzCmh8cnVnbXVuY2hlcgpofHJ1Z211bmNoZXJzCmh8ZnVkZ2VwYWNrZXIKaHxmdWRn"
    "ZXBhY2tlcnMKdHx0cmFubnkKdHx0cmFubmllCnR8dHJhbm5pZXMKdHx0cmFubnlzCnR8c2hlbWFsZQp0fHNoZW1hbGVz"
    "CnR8bGFkeWJveQp0fGxhZHlib3lzCnR8dHJvb24KdHx0cm9vbnMKdHxkaWNrZ2lybAp0fGRpY2tnaXJscwp0fHRyYW5z"
    "dHJlbmRlcgp0fHRyYW5zdHJlbmRlcnM="
)

MIN_ELIDE = 5          # shorter words get no "a separator stands in for a letter" variant

# From this length up, a blocked word may be followed by ANY letters and still be masked, so
# `niggerxd` and `faggotlol` do not walk through the suffix rule. It cannot be the rule for SHORT
# entries, where ordinary English is full of words that merely begin with one: `spic` would take
# `spicy`, `jap` would take `Japan`, `abo` would take `about` and `homo` would take `homosexual`.
# Five is where that thins out - and the entries at five that still collide do not rely on the
# number: _ORDINARY below cuts the colliding continuation out of their tail by name.
MIN_LOOSE_TAIL = 5


def entries():
    """[(category, word)] - "r" racist, "h" homophobic. Decoded on demand; it is 136 short strings
    and the only caller that matters runs once, at import, to build the patterns."""
    text = base64.b64decode(_BLOCKLIST.encode("ascii")).decode("utf-8")
    out = []
    for line in text.splitlines():
        cat, _, word = line.partition("|")
        if word:
            out.append((cat, word))
    return out


def words(category=None):
    return [w for c, w in entries() if category is None or c == category]


# ---------------------------------------------------------------- normalising
def _fold(ch):
    """One character of input as zero or more plain ascii characters."""
    known = _CONFUSABLES.get(ch)
    if known is not None:
        return known
    decomposed = unicodedata.normalize("NFKD", ch)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    # casefold, not lower: it is what turns ß into ss, and it is the right answer for the
    # non-Latin scripts NFKD leaves behind.
    return stripped.casefold()


def _squash(text, leet_as_letters=True):
    """(stream, spans) - `text` as ascii letters with one SEP per run of anything else.

    `spans[i]` is the (start, end) slice of the ORIGINAL text that stream character `i` came from.
    A SEP's span covers its whole run, so masking a match swallows the separators inside it and
    `n i g g e r` comes back as eleven stars rather than six stars and five spaces."""
    stream, spans = [], []
    gap_start = None
    for i, ch in enumerate(text):
        for folded in _fold(ch):
            letter = ""
            if "a" <= folded <= "z":
                letter = folded
            elif leet_as_letters and folded in _LEET:
                letter = _LEET[folded]
            if letter:
                if gap_start is not None:
                    stream.append(SEP)
                    spans.append((gap_start, i))
                    gap_start = None
                stream.append(letter)
                spans.append((i, i + 1))
            elif gap_start is None:
                gap_start = i
    # a trailing run of separators is dropped: nothing can match past the last letter
    return "".join(stream), spans


# ---------------------------------------------------------------- the patterns
def _variants(word):
    """The regex branches for one blocked word: the plain one, then one per INTERIOR letter with
    that letter replaced by a required separator (`n*gger`). Never the first or last letter, so a
    match always begins and ends on a real letter and the boundary rules stay simple."""
    letters = [re.escape(c) + "+" for c in word]
    gap = re.escape(SEP) + "?"
    out = [gap.join(letters)]
    if len(word) >= MIN_ELIDE:
        for k in range(1, len(word) - 1):
            pieces = []
            for i, letter in enumerate(letters):
                if i == k:
                    pieces.append(re.escape(SEP))        # a separator IS the missing letter
                elif i and i - 1 != k:
                    pieces.append(gap + letter)          # ...and never two separators in a row,
                else:                                    # which the stream cannot contain anyway
                    pieces.append(letter)
            out.append("".join(pieces))
    return out


# Longer suffixes first so `-ers` wins over `-er`; the trailing lookahead is what turns the whole
# pattern into an end-of-word test rather than a substring test.
_SUFFIX_ALT = "(?:%s)?" % "|".join(sorted(_SUFFIXES, key=len, reverse=True))
_END = "(?=%s|$)" % re.escape(SEP)


def _collides(word):
    """The shortest continuations that turn this entry into an ordinary word: `nigga` -> `rd`
    (from niggard, niggards, niggardly, niggardliness), `homos` -> `exual`.

    SHORTEST, so the guard built from them is fail-safe. Refusing `rd` refuses every word that
    starts `niggard`, including the inflections nobody thought to list, which is the behaviour to
    want on this side of the line: a missed slur is a missed slur, a mangled `homosexuality` is the
    filter doing harm."""
    tails = sorted(w[len(word):] for w in _ORDINARY if w != word and w.startswith(word))
    shortest = []
    for tail in tails:
        if not any(tail.startswith(seen) for seen in shortest):
            shortest.append(tail)
    return shortest


def _branch(word):
    """One blocked word as a complete regex branch: its spellings, then whatever it is allowed to
    end with, then the end-of-word test.

    The tail is per word rather than per pattern, because it depends on the word itself. Short
    entries have to end at a boundary or a plain suffix (MIN_LOOSE_TAIL); longer ones may be
    followed by any letters, MINUS the continuations that would make them an ordinary word."""
    if len(word) < MIN_LOOSE_TAIL:
        tail = _SUFFIX_ALT
    else:
        guard = _collides(word)
        tail = ("(?!%s)" % "|".join(guard) if guard else "") + "[a-z]*"
    return "(?:%s)%s%s" % ("|".join(_variants(word)), tail, _END)


def _build():
    """{first letter: compiled regex}. One regex per starting letter rather than one big one, so a
    scan only runs the branches that could possibly match the character in front of it.

    LONGEST WORD FIRST inside each: alternation takes the first branch that matches, not the
    longest, so with `fag` ahead of `faggot` every `faggot` would be masked three letters short."""
    by_letter = {}
    for _cat, word in sorted(entries(), key=lambda e: (-len(e[1]), e[1])):
        by_letter.setdefault(word[0], []).append(_branch(word))
    return {letter: re.compile("|".join(branches)) for letter, branches in by_letter.items()}


# BUILT ON FIRST USE, not at import. It is 1152 branches and ~55 ms to compile, which is 55 ms of
# a cold hub start spent on something most sessions never reach - nobody types in the lobby chat
# before the lobby exists. Filtering a line then costs 0.12 ms.
_RX = None


def _patterns():
    global _RX
    if _RX is None:
        _RX = _build()
    return _RX


# ---------------------------------------------------------------- matching
def _spans(text, leet_as_letters):
    """The (start, end) slices of `text` that hold a blocked word, under one reading of it.

    Only word-boundary positions are tried - the start of the stream, and every letter following a
    separator - which is the front half of the anchoring rule and also why this is cheap."""
    stream, spans = _squash(text, leet_as_letters)
    found, i, n = [], 0, len(stream)
    while i < n:
        if stream[i] != SEP and (i == 0 or stream[i - 1] == SEP):
            rx = _patterns().get(stream[i])
            m = rx.match(stream, i) if rx is not None else None
            if m is not None:
                end = m.end()
                # The pattern ends at a boundary, so this IS the whole word the match sits in.
                if stream[i:end] not in _ORDINARY:
                    found.append((spans[i][0], spans[end - 1][1]))
                i = end
                continue
        i += 1
    return found


def censor(text, mask=MASK):
    """`text` with every blocked word replaced by `mask`, one character for each covered.

    Returned unchanged when there is nothing to mask, so a caller can compare it to find out
    whether anything was hit."""
    if not text:
        return text
    hits = _spans(text, True) + _spans(text, False)
    if not hits:
        return text
    out = list(text)
    for start, end in hits:
        for i in range(start, end):
            out[i] = mask
    return "".join(out)


def is_clean(text):
    """True when `text` carries nothing the filter would mask."""
    return not (_spans(text, True) or _spans(text, False))


# ---------------------------------------------------------------- maintenance
def _main(argv):
    if "--list" in argv:
        for cat, word in entries():
            print("%s %s" % (cat, word))
        return 0
    if "--encode" in argv:
        import textwrap
        lines = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
        blob = base64.b64encode("\n".join(lines).encode("utf-8")).decode("ascii")
        print("# %d entries" % len(lines))
        print("_BLOCKLIST = (")
        for chunk in textwrap.wrap(blob, 92):
            print('    "%s"' % chunk)
        print(")")
        return 0
    if "--check" in argv:
        for line in sys.stdin:
            print(censor(line.rstrip("\n")))
        return 0
    print("usage: python -m hub.censor [--list | --encode | --check]")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
