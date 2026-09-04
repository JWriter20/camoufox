#!/usr/bin/env python3
"""
Enforce that synthesized input is dispatched from exactly one place.

WHY THIS EXISTS
Between 2026-04 and 2026-09, four deadlocks shipped from the same invariant
being broken in four different ways -- exact-edge coordinates (#225), humanized
trajectory points that bypassed the endpoint's guard (#677), a zero-displacement
move, and the top-edge row (#751, #752). Each was fixed by adding one more
coordinate guard at one more call site.

The invariant:

    A synthesized input event whose ack we await must reach the content
    renderer -- and when it does not, we must stop waiting.

It is unenforceable by review, because a violation looks like ordinary
arithmetic and costs the entire browser process. #677 is the proof: restoring
the humanize trajectory meant writing a bounds check, and the check that got
written was a copy of the pre-#225 form -- reintroducing a fixed deadlock one
day before it was re-fixed. Nobody caught it in review; a grep would have.

So: one module owns the conversion, the bounds predicate and the ack wait, and
this check fails the build if anything else takes that job on. It needs no
browser build and runs in seconds, so it can gate every pull request.

    python3 scripts/check-input-dispatch.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOT = ROOT / "additions" / "juggler"
CHOKEPOINT = "additions/juggler/input/MouseDispatch.js"
DOC = "docs/input-dispatch.md"

# The two exemptions are both the CONTENT process -- the other end of the wire,
# where the chokepoint's job does not exist:
#
#   PageAgent  dispatches drag events with coordinates that are already
#              content-relative. No browser-element offset, no ack awaited.
#   FrameTree  is the ack PRODUCER: it observes the
#              juggler-mouse-event-hit-renderer notification and emits the
#              InputEvent carrying jugglerEventId. It waits for nothing.
#
# Both are narrow and deliberate. Anything in the parent process is covered.
CONTENT_DRAG = "additions/juggler/content/PageAgent.js"
CONTENT_ACK_SOURCE = "additions/juggler/content/FrameTree.js"

# (regex, what the code is doing, files exempt in addition to the chokepoint)
RULES = [
    (r"\bjugglerSendMouseEvent\s*\(", "dispatches a synthesized mouse event", {CONTENT_DRAG}),
    (r"\bsendWheelEvent\s*\(", "dispatches a synthesized wheel event", set()),
    (r"\bjugglerEventId\b", "waits for a renderer ack", {CONTENT_ACK_SOURCE}),
    (r"\bboundingBox\s*\.\s*(?:left|top)\b", "does browser-relative coordinate arithmetic", set()),
]

REMEDY = (
    f"Route it through MouseDispatch ({CHOKEPOINT}): sendAcked() to dispatch and\n"
    f"    wait under a deadline, isInViewport() for the bounds predicate,\n"
    f"    toAbsolute() for the conversion. See {DOC}."
)


def main() -> int:
    if not (ROOT / CHOKEPOINT).is_file():
        print(f"FAIL: the chokepoint {CHOKEPOINT} is missing.")
        print("      If it moved, update CHOKEPOINT in this script and in " + DOC + ".")
        return 1

    compiled = [(re.compile(p), what, exempt) for p, what, exempt in RULES]
    violations = []

    for path in sorted(SCAN_ROOT.rglob("*.js")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == CHOKEPOINT or path.name.endswith(".bak"):
            continue
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith(("//", "*", "/*")):
                continue
            for pattern, what, exempt in compiled:
                if rel in exempt:
                    continue
                if pattern.search(line):
                    violations.append((rel, lineno, what, line.strip()))

    if not violations:
        scanned = sum(1 for _ in SCAN_ROOT.rglob("*.js"))
        print(f"input-dispatch: ok -- {scanned} files scanned, all synthesized input "
              f"goes through {CHOKEPOINT}")
        return 0

    print("input-dispatch: FAILED\n")
    for rel, lineno, what, line in violations:
        print(f"  {rel}:{lineno} {what} outside the chokepoint")
        print(f"    {line}")
    print(f"\n    {REMEDY}")
    print(f"\n{len(violations)} violation(s).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
