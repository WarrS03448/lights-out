#!/usr/bin/env python3.12
"""Counts in seven languages: the noun agrees with the number.  Run:

    python3.12 tests/test_i18n_plurals.py

"1 more placement matches required for rank calibration" was on screen in the hero, the result
screen, the post-match card, the profile and the Tk fallback, because a key had one string and a
count next to a noun needs the noun's form for THAT count. The seven languages do not agree on
how many forms there are (Russian has three, and got 2, 3, 4, 21, 22... wrong on the top bar as
well), so hub/i18n.py gained plural_form, tn() and a PLURALS table beside STRINGS, and the web UI
gained HubUI.tn. The JS rule is a mirror of the Python one, so the node check below holds the
two to the same answer for every count up to a thousand.

Sam gave the English placement line's shape himself ("X more placement matches required for
rank calibration", tests/test_screen_hero.py). It is unchanged; only its "1" form is new.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("HUB_STATE_DIR", tempfile.mkdtemp(prefix="hub-plural-state-"))
sys.path.insert(0, str(REPO))

from hub import i18n                                   # noqa: E402

RESULTS = []
STATIC = REPO / "hub" / "webui" / "static"
PH = re.compile(r"\{(\w+)\}")
# The forms each language's rule can produce besides the general one.
REACHABLE = {"en": {"one"}, "de": {"one"}, "es": {"one"}, "fr": {"one"}, "pt": {"one"},
             "ru": {"one", "few"}, "zh": set()}


def test_each_language_counts_by_its_own_rule():
    cases = {
        "en": {0: "other", 1: "one", 2: "other", 11: "other", 21: "other", 101: "other"},
        "de": {0: "other", 1: "one", 2: "other", 21: "other"},
        "es": {0: "other", 1: "one", 2: "other", 21: "other"},
        "pt": {0: "other", 1: "one", 2: "other", 21: "other"},
        "fr": {0: "one", 1: "one", 2: "other", 21: "other"},
        "ru": {0: "other", 1: "one", 2: "few", 3: "few", 4: "few", 5: "other", 11: "other",
               12: "other", 14: "other", 21: "one", 22: "few", 24: "few", 25: "other",
               101: "one", 111: "other", 112: "other", 1234: "few", 1234567: "other"},
        "zh": {0: "other", 1: "other", 2: "other"},
    }
    assert set(cases) == set(i18n.CODES)
    for code, table in cases.items():
        for n, form in table.items():
            assert i18n.plural_form(code, n) == form, (code, n, i18n.plural_form(code, n), form)
    # Not a whole number - the top bar's dash for a total not known yet - is the general form.
    for n in (None, "-", "", True, 2.5, float("nan")):
        assert i18n.plural_form("ru", n) == "other", n
    assert i18n.plural_form("ru", -21) == "one" and i18n.plural_form("en", 1.0) == "one"


def test_the_placement_line_agrees_with_its_count():
    general = i18n.STRINGS["en"]["comp_placements_left"]
    assert general == "{n} more placement matches required for rank calibration", "Sam's shape"
    assert i18n.trn("en", "comp_placements_left", 1) == "1 more placement match required for rank calibration"
    assert i18n.trn("en", "comp_placements_left", 3) == general.format(n=3)
    ru = {1: "Ещё 1 квалификационный матч для калибровки ранга",
          2: "Ещё 2 квалификационных матча для калибровки ранга",
          5: "Ещё 5 квалификационных матчей для калибровки ранга",
          21: "Ещё 21 квалификационный матч для калибровки ранга",
          22: "Ещё 22 квалификационных матча для калибровки ранга"}
    for n, text in ru.items():
        assert i18n.trn("ru", "comp_placements_left", n) == text, (n, i18n.trn("ru", "comp_placements_left", n))
    # Every language with a singular says 1 differently from 2; Chinese says it the same way.
    for code in i18n.CODES:
        one, two = (i18n.trn(code, "comp_placements_left", n).replace(str(n), "#") for n in (1, 2))
        assert (one == two) == (code == "zh"), (code, one, two)
    # tn() is trn() in the current language
    before = i18n.get_language()
    try:
        i18n.set_language("de")
        assert i18n.tn("comp_placements_left", 1) == "Noch 1 Platzierungsspiel für die Rangkalibrierung"
    finally:
        i18n.set_language(before)


def test_every_form_is_one_its_language_can_reach():
    """Only forms that DIFFER from the general one, for keys that exist, keeping its
    placeholders: a form nothing can select, or one missing a {placeholder}, is a KeyError
    or a dead string waiting for the one language that has it."""
    assert set(i18n.PLURALS) == set(i18n.CODES)
    for code, keys in i18n.PLURALS.items():
        for key, forms in keys.items():
            general = i18n.STRINGS[code].get(key)
            assert general, (code, key, "no general form in STRINGS")
            assert set(forms) <= REACHABLE[code], (code, key, set(forms) - REACHABLE[code])
            for form, text in forms.items():
                assert text.strip() and text != general, (code, key, form)
                assert set(PH.findall(text)) == set(PH.findall(general)), (code, key, form)
                assert "—" not in text, ("em dash in hub text", code, key, form)
    # The lines this was written for, in every language that needs them.
    for key in ("comp_placements_left", "rank_placements", "comp_record_played", "comp_record_wins"):
        for code in ("en", "de", "es", "fr", "pt", "ru"):
            assert "one" in i18n.PLURALS[code].get(key, {}), (code, key)
        assert "few" in i18n.PLURALS["ru"][key], key
    for key in ("topbar_registered", "topbar_tournament"):
        assert set(i18n.PLURALS["ru"][key]) == {"one", "few"}, key


def test_the_record_counts_both_numbers():
    """Two counts in one line ("1 match · 0 wins"), so the line is two fragments, each agreeing
    with its own number. The general forms still read exactly as the line did before."""
    for code in i18n.CODES:
        assert set(PH.findall(i18n.STRINGS[code]["comp_record"])) == {"played", "wins"}, code
    record = lambda code, p, w: i18n.STRINGS[code]["comp_record"].format(
        played=i18n.trn(code, "comp_record_played", p), wins=i18n.trn(code, "comp_record_wins", w))
    assert record("en", 34, 19) == "34 matches · 19 wins"
    assert record("en", 1, 1) == "1 match · 1 win" and record("en", 1, 0) == "1 match · 0 wins"
    assert record("ru", 22, 21) == "22 матча · 21 победа"
    assert record("zh", 34, 19) == "34 场比赛 · 19 胜"


def test_the_web_ui_gets_its_own_languages_forms():
    from hub.webui import snapshot
    from hub.webui.screens import postmatch
    ru, de, zh = (snapshot.strings_for(code) for code in ("ru", "de", "zh"))
    assert ru["comp_placements_left|one"] == i18n.PLURALS["ru"]["comp_placements_left"]["one"]
    assert ru["comp_placements_left|few"] == i18n.PLURALS["ru"]["comp_placements_left"]["few"]
    assert ru["topbar_tournament|few"] == "{n} регистрации на турнир"
    assert "comp_placements_left|one" in de and "comp_placements_left|few" not in de
    assert not [k for k in zh if "|" in k], "Chinese has one form, so nothing ships beside it"
    # never English-filled: a German window without a form reads German, not English
    assert "topbar_registered|one" not in de
    # the general forms are untouched, so t() everywhere reads what it read before
    assert ru["comp_placements_left"] == i18n.STRINGS["ru"]["comp_placements_left"]
    # the post-match card reads its own table, so the forms of its shared line ride in it
    card = postmatch.strings_for("ru")
    assert card["placements_left"] == i18n.STRINGS["ru"]["comp_placements_left"]
    assert card["placements_left|few"] == i18n.PLURALS["ru"]["comp_placements_left"]["few"]


def test_every_count_goes_through_tn():
    """Each place that prints one of these counts asks for the count's form. A t() left behind
    prints the general form, which is exactly the "1 ... matches" this fixed."""
    comp_js = (STATIC / "screens" / "competitive.js").read_text(encoding="utf-8")
    profile_js = (STATIC / "screens" / "profile.js").read_text(encoding="utf-8")
    postmatch_js = (STATIC / "screens" / "postmatch.js").read_text(encoding="utf-8")
    core_js = (STATIC / "core.js").read_text(encoding="utf-8")
    comp_py = (REPO / "hub" / "competitive.py").read_text(encoding="utf-8")
    for key in ("comp_placements_left", "rank_placements"):
        assert 't("%s"' % key not in comp_js, key
    assert comp_js.count('tn("comp_placements_left", ') == 2 and 'tn("rank_placements", ' in comp_js
    assert 'tn("comp_placements_left", auth.placements_left)' in profile_js
    assert 't("comp_placements_left"' not in profile_js
    assert 'psn("placements_left", card.placements_left)' in postmatch_js
    assert 'ps("placements_left"' not in postmatch_js
    for key in ("topbar_registered", "topbar_tournament"):
        assert 'tn("%s", ' % key in core_js and 't("%s"' % key not in core_js, key
    assert "HubUI.setLanguage(state.lang)" in core_js, "the rule needs the window's language"
    for key in ("comp_placements_left", "comp_profile_sample", "comp_history_count",
                "comp_profile_unscored"):
        assert 't("%s"' % key not in comp_py and 'tn("%s", ' % key in comp_py, key
    assert 'tn("comp_record_played", played)' in comp_py and 'tn("comp_record_wins", wins)' in comp_py


def test_the_tk_rank_line_agrees():
    from hub import competitive as C
    before = i18n.get_language()
    try:
        i18n.set_language("en")
        line = C.CompetitivePanel._rank_text(None, {"placing": True, "placements_left": 1})
        assert line == "1 more placement match required for rank calibration", line
        i18n.set_language("ru")
        line = C.CompetitivePanel._rank_text(None, {"placing": True, "placements_left": 3})
        assert line == "Ещё 3 квалификационных матча для калибровки ранга", line
    finally:
        i18n.set_language(before)


def test_the_js_rule_is_the_python_rule():
    """HubUI.pluralForm runs under node exactly as the page loads it, against plural_form, for
    every count up to 1000 in every language, plus the values that are not counts."""
    node = shutil.which("node")
    if not node:
        # A real skip, never a quiet return: a return reads as a PASS, which is exactly how the
        # xvfb UI tests went unrun for weeks, and this is the one check holding JS to Python.
        raise unittest.SkipTest("no node, so the JS rule was NOT checked against the Python one")
    values = list(range(0, 1001)) + [1001, 1011, 1021, 1234, 1234567, -1, -2, -21, 2.5, 3.0,
                                     "7", "22", "-", "", None, True, False]
    expected = {code: [i18n.plural_form(code, v) for v in values] for code in i18n.CODES}
    table = {"k": "{n} things", "k|one": "{n} thing", "k|few": "{n} thingies"}
    script = """
global.window = {};
require(%s);
const H = window.HubUI, values = %s, expected = %s, bad = [];
for (const code of Object.keys(expected)) {
  values.forEach((v, i) => { const got = H.pluralForm(code, v);
    if (got !== expected[code][i]) bad.push([code, v, got, expected[code][i]]); });
}
H.setStrings(%s);
H.setLanguage("en");
const en = [H.tn("k", 1), H.tn("k", 2), H.tn("k", "-"), H.t("k", { n: 1 })];
H.setLanguage("ru");
const ru = [H.tn("k", 21), H.tn("k", 23), H.tn("k", 25), H.plural({ k: "{n} x" }, "k", 1)];
console.log(JSON.stringify({ bad: bad.slice(0, 20), count: bad.length, en: en, ru: ru }));
""" % (json.dumps(str(STATIC / "ui.js")), json.dumps(values), json.dumps(expected), json.dumps(table))
    # over stdin: a thousand counts in seven languages is past Windows' command-line limit
    r = subprocess.run([node, "-"], input=script, capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["count"] == 0, ("JS and Python disagree", out["bad"])
    assert out["en"] == ["1 thing", "2 things", "- things", "1 things"], out["en"]
    assert out["ru"] == ["21 thing", "23 thingies", "25 things", "{n} x"], out["ru"]


def _run(fn):
    try:
        fn()
        RESULTS.append((fn.__name__, "ok"))
        print("ok   %s" % fn.__name__)
    except unittest.SkipTest as skip:
        RESULTS.append((fn.__name__, "skip"))
        print("SKIP %s: %s" % (fn.__name__, skip))
    except Exception:  # noqa: BLE001
        RESULTS.append((fn.__name__, "fail"))
        print("FAIL %s\n%s" % (fn.__name__, traceback.format_exc()))


def main():
    for fn in [
        test_each_language_counts_by_its_own_rule,
        test_the_placement_line_agrees_with_its_count,
        test_every_form_is_one_its_language_can_reach,
        test_the_record_counts_both_numbers,
        test_the_web_ui_gets_its_own_languages_forms,
        test_every_count_goes_through_tn,
        test_the_tk_rank_line_agrees,
        test_the_js_rule_is_the_python_rule,
    ]:
        _run(fn)
    failed = [n for n, r in RESULTS if r == "fail"]
    skipped = [n for n, r in RESULTS if r == "skip"]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed) - len(skipped), len(RESULTS)))
    if skipped:
        print("SKIPPED (not passed): " + ", ".join(skipped))
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
