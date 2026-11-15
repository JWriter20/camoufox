/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

"use strict";

/**
 * The `notches` scroll engine: scroll the way a physical wheel does.
 *
 * Each notch is its own event of 3 LINES carrying one native tick, so the page
 * sees deltaMode 1, DOMMouseScroll.detail 3 and wheelDelta -120 per notch, and
 * a longer scroll arrives as several such events a few tens of ms apart.
 * Playwright's pixel delta gives detail = 100 and no line delta; lines without
 * ticks give wheelDelta -396 for one notch and one big event for several
 * (measured against XTEST input). 100 px == one notch.
 *
 * A macOS identity (`ctx.macOS`) turns macOS's wheel instead: lines at 16 px
 * each, without the native tick, starting at one line and growing
 * (docs/input-dispatch.md).
 *
 * Quantising into notches changes the number the page sees, so this engine is
 * opt-in: with scroll `raw`, mouse.wheel(0, 100) delivers deltaY 100 in
 * deltaMode 0, the delta the caller asked for and what upstream's suite asserts.
 */

const kPxPerNotch = 100;
const kLinesPerNotch = 3;
const kPxPerMacLine = 16;
const kMaxMacLines = 12;

function toNotches(delta) {
  const count = delta === 0 ? 0 : Math.max(1, Math.round(Math.abs(delta) / kPxPerNotch));
  return new Array(count).fill(Math.sign(delta) * kLinesPerNotch);
}

// macOS accelerates a turn: every recorded burst starts at one line and grows
// (docs/input-dispatch.md).
function toMacLines(delta, rng) {
  let left = delta === 0 ? 0 : Math.max(1, Math.round(Math.abs(delta) / kPxPerMacLine));
  const events = [];
  for (let size = 1; left > 0; size = Math.min(kMaxMacLines, size + 1 + Math.floor(rng() * 3))) {
    const lines = Math.min(size, left);
    events.push(Math.sign(delta) * lines);
    left -= lines;
  }
  return events;
}

export const notches = {
  /**
   * @param ctx the seam's plan context; `ctx.rng` and `ctx.macOS` are used.
   * @param at {x, y} where the wheel turns, browser-relative.
   * @param delta {deltaX, deltaY, deltaZ} as Page.dispatchWheelEvent got them.
   */
  planWheel(ctx, at, {deltaX, deltaY, deltaZ}) {
    const eventsFor = delta => (ctx.macOS ? toMacLines(delta, ctx.rng) : toNotches(delta));
    const xs = eventsFor(deltaX);
    const ys = eventsFor(deltaY);
    const count = Math.max(1, xs.length, ys.length);
    const steps = [];
    let t = 0;
    for (let i = 0; i < count; i++) {
      if (i)
        t += 18 + ctx.rng() * 42;
      const dx = xs[i] ?? 0;
      const dy = ys[i] ?? 0;
      steps.push({
        kind: 'wheel', t, x: at.x, y: at.y,
        dx, dy, dz: i ? 0 : deltaZ,
        mode: 1 /* WheelEvent.DOM_DELTA_LINE */,
        ticks: {x: dx, y: dy},
        // Every notch carries part of the requested distance.
        essential: true,
      });
    }
    return {steps, endState: {}};
  },
};
