"""
Verify speechSynthesis.speak() on a spoofed voice behaves like a real voice.

A spoofed voice has no speech engine behind it. voice-spoofing.patch used to fire
an `error` event for it unless `voices:fakeCompletion` was set, and even then it
fired `start` and `end` in the same instant. A real voice never errors on a plain
utterance, and its `end` arrives after the text has been spoken. Both were tells.

The host's own speech backend must not matter either. On a Linux host where
speech-dispatcher cannot start, Firefox broadcasts a voices error, and every
page used to error its queued utterances. A spoofed Windows identity's voices
do not depend on the host's daemon, so the guard runs with it unreachable.

What PASS means:
    * the page sees spoofed voices at all (so the check is not vacuous);
    * speaking six words (25 characters) on one fires `start`, then `end`, and
      no `error`; a Windows identity's voice fires a `sentence` boundary and a
      `word` boundary at each word's start in between, as SAPI does
      (windows/SapiService.cpp; stock Firefox 158 on win-i9 fires `boundary`
      for a one-word utterance, cfparity contracts.speech). A Linux identity's
      fires none, as speech-dispatcher's;
    * `end` arrives about as long after `start` as the text takes to say at
      ~150 words per minute (2s here), not in the same tick.

    python browser/tests/playwright/patches/spoofed-voice-speaks.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

TEXT = "aaa bbb cc dd eee ffff"  # 25 chars at 12.5 chars/s = 2.0s; words at 0,4,8,11,14,18
PROBE = """async (text) => {
  let voices = speechSynthesis.getVoices();
  if (!voices.length) {
    await new Promise(r => { speechSynthesis.onvoiceschanged = r; setTimeout(r, 3000); });
    voices = speechSynthesis.getVoices();
  }
  if (!voices.length) return {voices: 0};
  const u = new SpeechSynthesisUtterance(text);
  u.voice = voices[0];
  const events = [];
  const t0 = performance.now();
  const done = new Promise(resolve => {
    for (const type of ['start', 'end', 'error', 'boundary']) {
      u.addEventListener(type, (e) => {
        events.push([type === 'boundary' ? `${e.name}@${e.charIndex}+${e.charLength}` : type, Math.round(performance.now() - t0)]);
        if (type === 'end' || type === 'error') resolve();
      });
    }
    setTimeout(resolve, 10000);
  });
  speechSynthesis.speak(u);
  await done;
  return {voices: voices.length, events};
}"""


async def speak(claimed, text=TEXT):
    from camoufox.async_api import AsyncCamoufox

    # An unreachable speech-dispatcher socket: the host backend fails to start,
    # as it does on a machine without the daemon.
    env = {**os.environ, "SPEECHD_ADDRESS": "unix_socket:/nonexistent/speechd.sock"}
    async with AsyncCamoufox(headless=True, os=claimed, env=env, i_know_what_im_doing=True,
                             executable_path=str(resolve_binary())) as browser:
        page = await browser.new_page()
        await page.goto("about:blank")
        return await page.evaluate(PROBE, text)


WINDOWS_BOUNDARIES = ["sentence@0+22", "word@0+3", "word@4+3", "word@8+2", "word@11+2", "word@14+3", "word@18+4"]
# Stock Firefox 158 on win-i9 (Microsoft David, rate 1): sentences end at . ! ?, a word's
# charLength leaves out trailing punctuation, and "3.14" is four spoken words at one index.
STOCK_TEXT = "don't stop-now, 3.14 ok. Fine"
STOCK_BOUNDARIES = ["sentence@0+24", "word@0+5", "word@6+8", "word@16+4", "word@16+4", "word@16+4", "word@16+4",
                    "word@21+2", "sentence@25+4", "word@25+4"]


async def main() -> int:
    failures = []
    for claimed, between in (("windows", WINDOWS_BOUNDARIES), ("linux", [])):
        got = await speak(claimed)
        print(f"  {claimed:8} {got}")
        if not got["voices"]:
            failures.append(f"claiming {claimed}: the page sees no voices, so nothing was tested")
            continue
        kinds = [kind for kind, _ in got["events"]]
        if kinds != ["start", *between, "end"]:
            failures.append(f"claiming {claimed}: expected {['start', *between, 'end']}, got {kinds}")
            continue
        spoken = got["events"][-1][1] - got["events"][0][1]
        if not 1500 <= spoken <= 4000:
            failures.append(f"claiming {claimed}: end came {spoken}ms after start, expected about 2000ms")
    got = await speak("windows", STOCK_TEXT)
    print(f"  windows  {got}")
    kinds = [kind for kind, _ in got.get("events", [])]
    if kinds != ["start", *STOCK_BOUNDARIES, "end"]:
        failures.append(f"claiming windows, {STOCK_TEXT!r}: expected {['start', *STOCK_BOUNDARIES, 'end']}, got {kinds}")
    elif got["events"][0][1] < 50:
        failures.append(f"claiming windows: start came {got['events'][0][1]}ms after speak(), SAPI's comes ~100ms later")
    for failure in failures:
        print(f"  FAIL: {failure}")
    if failures:
        return 1
    print("  PASS: start, Windows's word boundaries for a Windows voice only, then end about 2s later, no error")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
