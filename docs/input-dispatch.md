# Synthesized input dispatch

Every synthesized mouse and wheel event in the parent process goes through
`additions/juggler/input/MouseDispatch.js`. `scripts/check-input-dispatch.py`
fails the build if anything else dispatches input or does browser-relative
coordinate arithmetic, and it runs on every pull request.

## The invariant

> A synthesized input event whose ack we await must reach the content
> renderer — and when it does not, we must stop waiting.

## Why it is worth a module and a lint

Four deadlocks shipped between 2026-04 and 2026-09, all the same failure:

| Date | Commit | Trigger |
|---|---|---|
| 2026-06-04 | `9270618` | `x == width` / `y == height` — the far edges |
| 2026-07-18 | `541ffca` | trajectory points, which bypassed the endpoint's guard (#225, #677) |
| 2026-07-24 | `16e5a13` | a zero-displacement move |
| 2026-09-03 | `014cc65` | `y == 0` — the near edge (#751, #752) |

Each was fixed by adding one more coordinate guard at one more call site. That
does not converge, for two reasons.

**The trigger set is not enumerable.** Whether relative `y == 0` reaches the
renderer is decided by `Math.round(boundingBox.top) < boundingBox.top` — a
rounding accident in the fractional height of browser chrome, which varies with
the *spoofed OS*: windows `51.4` → `51` deadlocks, macos `53.1` → `53`
deadlocks, linux `56.5` → `57` is fine. No review catches that, and no
hand-written list of coordinates contains it.

**Every miss costs the whole process.** `activateAndRun()`
(`TargetRegistry.js`) serializes input on a promise chain shared by every tab in
the process. It swallows errors to keep the chain running, but it cannot swallow
a callback that never returns. One unbounded `await` for an ack that will never
arrive wedges every later input event, in every tab, permanently — at 0% CPU,
with nothing in flight and no diagnostic.

`#677` is why review is not enough: restoring the humanize trajectory meant
writing a bounds check, and the one written was a copy of the pre-`#225` form,
reintroducing a fixed deadlock one day before it was re-fixed.

## How it is enforced

**One chokepoint.** `MouseDispatch` owns the relative→absolute conversion (with
the boundary snap), the in-viewport predicate, and the ack wait. Callers pass
relative coordinates and never see a bounding box.

**Bounded waits.** `sendAcked()` waits at most `kAckDeadlineMs` (5s) and then
drops the event with a warning naming the type, coordinate and browser rect. The
deadline is sized above the slowest *legitimate* ack, not near the typical one:
acks are p99 1ms on an idle page, but they are delivered from the content main
thread and inherit any block on it — a 3s synchronous script delayed one by
2849ms. `sendTrajectoryAcked()` abandons the rest of a curve after the first
undelivered point, so ~110 bounded waits cannot add up to an unbounded slot.
`activateAndRun()` carries a 30s backstop for the other unbounded waits
reachable from the same slot (`apz-repaints-flushed`, `TabSwitchDone`, the drag
path's waits), none of which has failed yet.

**The static check.** `scripts/check-input-dispatch.py`, wired into
`.github/workflows/lint.yml`. Two exemptions, both content-process:
`PageAgent.js` (drag events, already content-relative, no ack) and
`FrameTree.js` (the ack *producer*).

**Boundary coverage.** `tests/patches/mouse-boundary-sweep.py` sweeps the whole
viewport ring across every spoofed OS with humanize on and off, asserting each
point is acked *and observed by the page*. Hand-picked coordinate lists are what
let each of the four bugs through: `humanize-edge-deadlock.py` probed only the
far edges, and `humanize-mouse-trajectory.py` pins `os="linux"` — the one
fingerprint immune to `#751`. `tests/patches/input-ack-backstop.py` covers the
bounded wait itself.
