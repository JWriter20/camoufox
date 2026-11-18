# Humanized input

The browser does not humanize input. It delivers what the driver sends, as
sent (docs/input-dispatch.md, "Input as sent"): no cursor path, no key or
button hold, no pacing. Shaping input is done in the driver.

## `custom(fn)`

`humanize={"mouse": custom(fn), "keyboard": custom(fn), "scroll": custom(fn)}`
wraps Playwright's input methods on the client
(`python/src/camoufox/_humanize_custom.py`, `typescript/src/humanize-custom.ts`):
the function plans the steps and the launcher sends them as ordinary Playwright
calls. User code never runs in the browser, and the launcher writes no config
key for it. The browser guard is
`browser/tests/playwright/patches/humanize-custom.py`.

## `humanize-engines.json`

Each build ships one beside `properties.json`, listing the engines the browser
itself runs per channel. This repository's lists none:

```json
{"version": 2, "auto": {}, "engines": {}}
```

On such a build `humanize=True`, a number, or a named engine is refused with
`HumanizeEngineUnavailable`; nothing is silently ignored. `False`, `raw()` and
`custom(fn)` are accepted.

Older builds planned the mouse channel in the browser. Their file lists a
channel's engines (`"mouse": ["raw", "cursory"]`), what `auto()` tries, and
each engine's option keys; the launchers still read that and write those
builds' `humanize*` keys. A build without the file is older still and takes
only `humanize` and `humanize:maxTime`/`minTime`.

## Seeds

`seed` in the `humanize` dict is a 64-bit integer. Each channel's stream is
`Mulberry32(splitmix64(seed ^ tag) & 0xffffffff)`, with tags `0x6d6f7573`
(mouse), `0x6b657962` (keyboard) and `0x7363726f` (scroll); a `custom()`
engine draws from its channel's. Without a seed, each launch draws a random
one. Both launchers derive the same streams (`channel_stream` in
`python/src/camoufox/humanize.py`, `channelStream` in
`typescript/src/humanize.ts`) and are tested against
`browser/tests/humanize/rng-vectors.json`: change the derivation in both or in
neither.
