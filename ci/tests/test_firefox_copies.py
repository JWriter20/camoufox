"""browser/scripts/check-firefox-copies.py: the copied fallback tables are
compared statement by statement, and only the documented additions are left
out. The comparison against the release itself runs in the static job.

Run:  python3 -m pytest ci/tests -q
"""

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location(
        "check_firefox_copies", REPO / "browser" / "scripts" / "check-firefox-copies.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch_lines(check):
    return [line[1:] for line in check.PATCH.read_text(encoding="utf-8").splitlines() if line.startswith("+")]


def test_the_patch_marks_exactly_the_documented_additions(check):
    found = set()
    for copy, (ours, _) in check.COPIES.items():
        check.statements(check.body(_patch_lines(check), ours), copy, found)
    assert found == check.ADDITIONS


def _stock(check, copy):
    """The copy as stock would have it: the marked additions taken out."""
    ours, theirs = check.COPIES[copy]
    lines = check.body(_patch_lines(check), ours)
    kept, skip = [], False
    for line in lines:
        if line.strip().startswith("// Camoufox:"):
            skip = True
            continue
        if skip and line.strip() and not line.strip().startswith("//"):
            skip = False
            continue
        if not skip:
            kept.append(line)
    return [f"{theirs}uint32_t aCh, Script aRunScript, FontPresentation aPresentation,",
            "    nsTArray<const char*>& aFontList) {", *kept, "}"]


@pytest.mark.parametrize("copy", ["windows", "mac"])
def test_a_matching_release_passes_and_a_changed_one_fails(check, copy):
    ours, theirs = check.COPIES[copy]
    mine = check.statements(check.body(_patch_lines(check), ours), copy, set())
    stock = _stock(check, copy)
    assert check.statements(check.body(stock, theirs)) == mine
    # Stock renames one face: the copy is stale.
    first = next(n for n, line in enumerate(stock) if 'AppendElement("' in line)
    changed = stock[:first] + [stock[first].replace('("', '("Renamed ', 1)] + stock[first + 1:]
    assert changed != stock
    assert check.statements(check.body(changed, theirs)) != mine


def test_comments_and_spacing_do_not_count(check):
    a = ['  case Script::LAO:', '    aFontList.AppendElement("Lao UI");  // stock', '    break;']
    b = ['case   Script::LAO:', '// a comment', '', 'aFontList.AppendElement("Lao UI");', 'break;']
    assert check.statements(a) == check.statements(b)
