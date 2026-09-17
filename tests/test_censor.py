# -*- coding: utf-8 -*-
"""The chat filter: hub/censor.py.

NO SLUR IS SPELLED OUT IN THIS FILE. Every case is built from the module's own blocklist, which is
what the filter has to catch anyway - so these say "every listed word, mangled this way, is still
caught" rather than naming any of them. The words that must NOT be caught are ordinary English and
are written out in full, because that half matters exactly as much: a filter that eats
`Scunthorpe`, `assassin` or `spices` gets switched off, and then it catches nothing at all.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import censor                                            # noqa: E402

WORDS = censor.words()
LONG = [w for w in WORDS if len(w) >= censor.MIN_ELIDE]

# a -> 4, e -> 3 ... one substitute per letter, the reverse of censor._LEET
LEET = {}
for _sym, _letter in censor._LEET.items():
    LEET.setdefault(_letter, _sym)

CONFUSABLE = {}
for _char, _letter in censor._CONFUSABLES.items():
    CONFUSABLE.setdefault(_letter, _char)


def masked(text):
    """True when censor() replaced every character of `text`."""
    out = censor.censor(text)
    return out != text and all(c == censor.MASK for c in out)


def masked_run(out, least):
    """True when `out` holds a run of at least `least` mask characters."""
    return censor.MASK * least in out


# ---------------------------------------------------------------- the list itself
CATEGORIES = ("r", "h", "t")            # racist, homophobic, transphobic


def test_the_blocklist_decodes():
    assert len(WORDS) > 100
    assert len(WORDS) == len(set(WORDS)), "duplicate entry"
    assert all(w.isalpha() and w.islower() for w in WORDS), "entries are plain lowercase letters"
    assert all(censor.words(cat) for cat in CATEGORIES), "an empty category"
    assert sum(len(censor.words(cat)) for cat in CATEGORIES) == len(WORDS), "an entry with no category"


def test_all_three_kinds_of_slur_are_covered():
    """The scope, asserted rather than assumed. Transphobic slurs are their own category rather
    than a few entries filed under homophobic (Sam, 2026-09-16: "make transphobic slurs be
    censored too"), so a later edit to one group cannot quietly empty another."""
    for cat in CATEGORIES:
        assert len(censor.words(cat)) >= 10, cat
        for word in censor.words(cat):
            assert masked(word), (cat, word)


# Words people use about THEMSELVES, and the neutral vocabulary around the same subjects. A filter
# that masks these in a lobby where somebody is being called names is worse than no filter at all.
SELF_DESCRIBED = [
    "trans", "transgender", "transgendered", "transsexual", "transsexuals",
    "transition", "transitions", "transitioned", "transitioning",
    "transfeminine", "transmasculine", "transphobe", "transphobia", "transphobic",
    "homosexual", "homosexuals", "homosexuality", "homophobia", "homophobic",
    "gay", "gays", "lesbian", "lesbians", "bisexual", "nonbinary", "enby",
    "femboy", "drag", "pride",
]


def test_the_words_people_use_about_themselves_are_never_masked():
    eaten = {w: censor.censor(w) for w in SELF_DESCRIBED if censor.censor(w) != w}
    assert not eaten, "the filter masked a self-description: %s" % eaten
    for line in ("I am trans btw", "she is transitioning", "gay guy here, gg wp"):
        assert censor.censor(line) == line, line


def test_the_words_the_game_uses_are_never_masked():
    """`trap` is the single most common word in a Bodybomb lobby - a bomb site, a corner, an
    ambush - and it is also used as a transphobic slur. It is deliberately NOT on the list, and
    this is what stops someone adding it in a hurry."""
    for line in ("its a trap", "trap the door", "watch the traps on B", "trapped"):
        assert censor.censor(line) == line, line


def test_every_blocked_word_is_caught_on_its_own_and_in_a_sentence():
    for word in WORDS:
        assert masked(word), word
        out = censor.censor("you are such a %s mate" % word)
        assert censor.MASK * len(word) in out, word
        assert out.startswith("you are such a ") and out.endswith(" mate"), word


def test_case_and_punctuation_do_not_help():
    for word in WORDS:
        assert masked(word.upper()), word
        assert masked(word.capitalize()), word
        # Punctuation may go under the mask with the word: `(` is another way of writing `c`
        # and `!` another way of writing `i`, so `(coon` reads as `ccoon` and `nigger!` as
        # `niggeri`, and each is masked as one run. Being generous about where the mask ENDS is
        # the safe direction to be wrong in, so these check the run, not the exact string.
        assert masked_run(censor.censor("%s!" % word), len(word)), word
        assert masked_run(censor.censor("(%s)" % word), len(word)), word


# ---------------------------------------------------------------- the bypasses
def test_spacing_and_punctuation_between_letters():
    for word in WORDS:
        for glue in (" ", ".", "-", "_", "*", " . ", "​"):   # the last is a zero-width space
            spaced = glue.join(word)
            assert masked(spaced), (word, glue)


def test_repeated_letters():
    for word in WORDS:
        assert masked("".join(c * 3 for c in word)), word


def test_leetspeak():
    for word in WORDS:
        leet = "".join(LEET.get(c, c) for c in word)
        if leet != word:
            assert masked(leet), word


def test_unicode_lookalikes_and_decorated_letters():
    for word in WORDS:
        cyrillic = "".join(CONFUSABLE.get(c, c) for c in word)
        if cyrillic != word:
            assert masked(cyrillic), word
        # fullwidth (ｆｕｌｌｗｉｄｔｈ) folds through NFKD
        assert masked("".join(chr(ord(c) - ord("a") + 0xFF41) for c in word)), word


def test_a_symbol_standing_in_for_a_letter():
    """`n*gger` - the most common way a player half-censors themselves, and still perfectly
    readable. Only for words long enough that a missing letter cannot invent a match (MIN_ELIDE)."""
    for word in LONG:
        for k in range(1, len(word) - 1):
            for sym in ("*", "#", "%"):
                bypass = word[:k] + sym + word[k + 1:]
                assert censor.censor(bypass) != bypass, (word, k, sym)


def test_digits_and_symbols_stuck_on_the_end():
    """Anything that is not a letter reads as a separator on the second pass, so it ends the word
    rather than hiding it - for every entry, however short."""
    for word in WORDS:
        for tail in ("1", "69", "!!!", "_", "~"):
            line = word + tail
            assert censor.censor(line) != line, (word, tail)


def test_letters_stuck_on_the_end_of_a_long_entry():
    """`niggerxd`. Only the longer entries: at three and four letters, ordinary English is full of
    words that merely begin with one (`Japan`, `about`, `spicy`, `homosexual`), and catching this
    for them would cost all of those. That is the documented limit, and this pins it in place."""
    for word in WORDS:
        for tail in ("xd", "lol", "123x"):
            line = word + tail
            if len(word) >= censor.MIN_LOOSE_TAIL:
                assert censor.censor(line) != line, (word, tail)


def test_plurals_and_ordinary_endings():
    for word in WORDS:
        for suffix in ("s", "es", "ing", "er"):
            if word + suffix in censor._ORDINARY:
                continue          # `jap` + `es` is the ordinary word `japes`; that is the point
            assert masked(word + suffix), (word, suffix)


def test_a_slur_buried_in_a_normal_message():
    """Spaced out in the middle of a sentence: masked, the line keeps its length, and what came
    before it is untouched. NOT what comes after - a match owns the separators inside it, so a
    spaced-out entry can run on into the next word (see the module's "known false positives")."""
    for word in WORDS:
        line = "gg team, %s, rematch?" % " ".join(word)
        out = censor.censor(line)
        assert out.startswith("gg team, "), word
        assert masked_run(out, len(word)) and len(out) == len(line), word


# ---------------------------------------------------------------- what must survive
# Every one of these contains, or reads like, something on the list. They are the reason the
# matcher anchors on word boundaries instead of looking for substrings.
INNOCENT = [
    "Scunthorpe", "assassin", "assassinate", "classic", "cockpit", "analysis", "Matsushita",
    "shiitake", "therapist", "skyscraper",
    "raccoon", "cocoon", "tycoon", "raccoons", "cocoons",
    "snigger", "sniggering", "niggardly", "bigger", "digger", "trigger",
    "Nigeria", "Nigerian", "negroni", "negronis",
    "Japan", "Japanese", "jape", "japes",
    "about", "above", "aboard", "abort", "abolish", "abomination",
    "squawk", "squawked", "squawking",
    "spice", "spices", "spiced", "spicy", "spicier", "spicing",
    "Pakistan", "Pakistani", "Pakistanis",
    "honking", "honked", "wiggle", "wiggled", "wiggler",
    "cooling", "coolant", "boondocks", "injury", "injured",
    "yield", "yielded", "flag", "flags", "flagging", "flagship",
    "homosexual", "homophobia", "homophobic", "homogeneous", "homework", "homer",
    "chinkapin", "chinkapins", "beanery", "beaneries", "beans", "greasy", "greased",
    "dagger", "daggers", "polar", "honest", "colander",
    "niggard", "niggards", "niggardly", "niggardliness",
    "homosexuals", "homosexuality", "homophobe", "homogenous", "homonym",
    "squawker", "squawkers", "negronis",
]


def test_every_protected_word_is_actually_protected():
    """_ORDINARY is the release valve, so it has to work by itself: every word in it survives, and
    it survives with the junk-tail rule still switched on for the entry it collides with."""
    eaten = {w: censor.censor(w) for w in censor._ORDINARY if censor.censor(w) != w}
    assert not eaten, "the filter ate a word it was told to protect: %s" % eaten
    collided = [w for w in WORDS if censor._collides(w)]
    assert collided, "nothing collides, so the mechanism is not being exercised at all"


def test_a_protected_word_does_not_switch_the_filter_off_for_that_entry():
    """The guard cuts out the continuation that makes an ordinary word, not the whole tail. The
    entry it protects still catches junk stuck on the end of it."""
    loose = [w for w in WORDS if censor._collides(w) and len(w) >= censor.MIN_LOOSE_TAIL]
    assert loose, "no entry is both long enough for the tail and colliding"
    for word in loose:
        assert censor.censor(word + "xd") != word + "xd", word


def test_unlisted_inflections_of_a_protected_word_are_safe_too():
    """The guard is built from the SHORTEST continuation, so it covers the forms nobody listed.
    `niggardliest` and `homosexualism` are not in _ORDINARY and must still come through."""
    for word in ("niggardliest", "niggardliness", "homosexualism", "homosexualities",
                 "pakistanian", "squawkiest", "negronied"):
        assert censor.censor(word) == word, word


def test_ordinary_words_are_left_alone():
    eaten = {w: censor.censor(w) for w in INNOCENT if censor.censor(w) != w}
    assert not eaten, "the filter ate ordinary words: %s" % eaten


def test_ordinary_sentences_are_left_alone():
    lines = [
        "gg wp, close one",
        "ban Rome, we always lose there",
        "I am going to Nigeria in the spring and my flight lands in Japan",
        "the assassin in the cockpit had a classic analysis of the spices",
        "he was sniggering about the raccoon in Scunthorpe",
        "",
        "   ",
        "??? !!! ...",
    ]
    for line in lines:
        assert censor.censor(line) == line, line


def test_is_clean_agrees_with_censor():
    for word in WORDS:
        assert not censor.is_clean(word), word
    for word in INNOCENT:
        assert censor.is_clean(word), word


def test_the_mask_is_the_same_length_as_what_it_covers():
    """One mask character per character covered, so a line never changes length and the rest of
    the message is still readable. The mask may run PAST the word - a separator inside a match is
    part of it, which is how `n i g g e r` is masked at all, and it means a following word can be
    swallowed when it completes another entry (`chink` + ` y` reads as one of them)."""
    for word in WORDS:
        line = "x %s y" % word
        out = censor.censor(line)
        assert len(out) == len(line), word
        assert out.startswith("x "), word
        assert out[2:2 + len(word)] == censor.MASK * len(word), word


def test_a_long_line_is_filtered_quickly():
    """A chat line is capped at 200 characters and the filter runs once per message, but the
    matcher is the only thing here that scales with what a player types."""
    import time
    line = ("gg wp everyone that was a close one, ban Rome next time " * 4)[:200]
    start = time.perf_counter()
    for _ in range(50):
        censor.censor(line)
    per_call = (time.perf_counter() - start) / 50
    assert per_call < 0.02, "%.1f ms per 200-character line" % (per_call * 1000)


# ---------------------------------------------------------------- the service's copy
# There are two implementations of this filter and there have to be: the hub ships frozen by
# PyInstaller, the service ships as server/ alone, and neither can import the other. The hub's copy
# keeps the word off the sender's screen; the SERVICE's copy is the one that runs before a line is
# broadcast to nine other people, which is the only place a patched client cannot reach.
#
# This is half of what stops them drifting - it reads the real file and compares every table.
# The other half is server/scripts/test-censor.mjs, which asserts the same behaviour the tests
# above do, generated from the same data.
def _server_data():
    """The DATA object out of server/censor.cjs, as Python."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "server", "censor.cjs")
    src = io.open(path, encoding="utf-8").read()
    start = src.index("const DATA = ") + len("const DATA = ")
    end = src.index(";" + chr(10), start)
    return json.loads(src[start:end])


def test_the_server_copy_carries_the_same_data():
    """Every table, field for field. A word added to one side and not the other is the failure
    this exists to make loud: the hub would mask it and the service would broadcast it."""
    data = _server_data()
    assert data["MASK"] == censor.MASK
    assert data["SEP"] == censor.SEP
    assert data["MIN_ELIDE"] == censor.MIN_ELIDE
    assert data["MIN_LOOSE_TAIL"] == censor.MIN_LOOSE_TAIL
    assert data["CONFUSABLES"] == dict(censor._CONFUSABLES)
    assert data["LEET"] == dict(censor._LEET)
    assert data["SUFFIXES"] == list(censor._SUFFIXES)
    assert sorted(data["ORDINARY"]) == sorted(censor._ORDINARY)
    assert data["BLOCKLIST"] == censor._BLOCKLIST, (
        "the service's blocklist is out of date; regenerate server/censor.cjs's DATA line")


def test_the_server_copy_decodes_to_the_same_words():
    """Not just the same blob - the same list once decoded, category for category."""
    import base64
    raw = base64.b64decode(_server_data()["BLOCKLIST"].encode("ascii")).decode("utf-8")
    theirs = [tuple(line.split("|", 1)) for line in raw.splitlines() if "|" in line]
    assert theirs == censor.entries()


# ---------------------------------------------------------------- in the lobby
def _session():
    from hub import competitive as C

    class FakePanel:
        def after(self, ms, fn):
            return None

    s = C.MockSession.__new__(C.MockSession)
    s.panel = FakePanel()
    s.me = {"name": "Sam", "steam_id": "1"}
    s.chat = {ch: [] for ch in C.CHAT_CHANNELS}
    s._chat_order = 0
    s._changed = lambda: None
    return C, s


def test_send_chat_masks_both_channels():
    """The line is filtered ON THE WAY IN, so no uncensored copy is ever in a log - including the
    sender's own, which is the point: they see what everyone else would have seen. Both channels,
    because a slur in team chat is still a slur."""
    _C, s = _session()
    word = WORDS[0]
    s.send_chat("gg %s" % word, "team")
    s.send_chat("gg %s" % word, "all")
    for channel in ("team", "all"):
        text = s.chat_log(channel)[-1]["text"]
        assert word not in text and censor.MASK in text, channel
        assert text.startswith("gg "), channel
    s.send_chat("gg wp", "all")
    assert s.chat_log("all")[-1]["text"] == "gg wp", "a clean line is untouched"


def test_send_chat_caps_the_length():
    """The 200-character limit is on the JS input, and the JS is the one part of this a player can
    edit. The session enforces it too, which also bounds what the filter is handed."""
    C, s = _session()
    s.send_chat("a" * 5000)
    assert len(s.chat_log("team")[-1]["text"]) == C.MAX_CHAT_CHARS
