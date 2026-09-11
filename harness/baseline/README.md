# Baseline

`current.json` is the floor every future run has to clear: the per-test outcomes
recorded from the last release that passed every gate. `harness/verify.py`
compares a run against it, and a test that passed here and does not pass now is
a regression regardless of what the totals did.

Identities are stable but not always readable. Playwright tests are recorded as
`async/test_page.py::test_foo`; build-tester checks as
`linux-per-context-0/core/Automation Detection/webdriver`; sundial vectors as a
20-character HMAC, because this repository is public and sundial's vectors are
not (see `../TRIBAL-KNOWLEDGE.md`).

## Recording one

Only from a run where every gate passed:

```bash
python3 -m harness.verify \
  --update-baseline \
  --firefox-version 153.0.4 \
  --camoufox-release beta.32 \
  --playwright-tag v1.62.0
```

`verify.py` refuses to record from a failing run. It cannot, however, tell you
whether a *passing* run passed for the right reasons — read the diff before you
commit a new baseline. A baseline recorded from a run where half a suite was
accidentally skipped will happily bless that same gap forever.

## The first one

Until a baseline exists the harness still runs every gate and still fails on a
gate that reports failure — it just cannot detect regressions, and says so.
Seed it from a known-good release rather than from the first green auto-update.
