"""
Tests for camoufox.locales -- normalising a locale tag into intl config keys.

Mirrors typescript/tests/locale.test.ts.

Run with:
    cd python && python -m pytest tests/test_locales.py -v

The core regression these guard: `locale:script` overrides
mozilla::intl::Locale::Script() for the whole browser, so emitting a
language's *implicit* suppress-script ("Latn" for "en") makes Firefox build
"en-Latn-US". That tag is not in ICU's available-locale set, so Intl falls
back to plain "en" while navigator.language stays "en-US" -- a mismatch a
page can read directly. Only an *explicit* script belongs in the config.
"""

import pytest

from camoufox.exceptions import InvalidLocale
from camoufox.locales import normalize_locale


class TestNormalizeLocale:
    def test_splits_language_and_region(self):
        locale = normalize_locale("en-US")
        assert locale.language == "en"
        assert locale.region == "US"
        assert locale.as_string == "en-US"

    def test_keeps_an_explicit_script(self):
        locale = normalize_locale("zh-Hans-CN")
        assert locale.language == "zh"
        assert locale.script == "Hans"
        assert locale.region == "CN"

    def test_does_not_invent_the_implicit_suppress_script(self):
        # "en" declares Suppress-Script: Latn. Emitting it would give
        # "en-Latn-US", which ICU does not know.
        assert normalize_locale("en-US").script is None
        assert normalize_locale("de-DE").script is None

    def test_rejects_a_locale_with_no_region(self):
        with pytest.raises(InvalidLocale):
            normalize_locale("en")

    def test_rejects_nonsense(self):
        with pytest.raises(InvalidLocale):
            normalize_locale("not a locale")


class TestAsConfig:
    def test_emits_the_intl_config_keys(self):
        assert normalize_locale("fr-FR").as_config() == {
            "locale:language": "fr",
            "locale:region": "FR",
        }

    def test_includes_an_explicit_script(self):
        assert normalize_locale("zh-Hant-TW").as_config() == {
            "locale:language": "zh",
            "locale:region": "TW",
            "locale:script": "Hant",
        }
