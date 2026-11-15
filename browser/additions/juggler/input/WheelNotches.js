/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

"use strict";

/**
 * The events one driven wheel call arrives as: what a physical wheel sends.
 *
 * Each notch is its own event of 3 LINES carrying one native tick, so the page
 * sees deltaMode 1, DOMMouseScroll.detail 3 and wheelDelta -120 per notch, and
 * a longer scroll arrives as several such events a few tens of ms apart.
 * Playwright's pixel delta gives detail = 100 and no line delta; lines without
 * ticks give wheelDelta -396 for one notch and one big event for several
 * (measured against XTEST input). 100 px == one notch.
 *
 * A macOS identity turns macOS's wheel instead: lines at 16 px each, without
 * the native tick, starting at one line and growing (docs/input-dispatch.md).
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
function toMacLines(delta) {
  let left = delta === 0 ? 0 : Math.max(1, Math.round(Math.abs(delta) / kPxPerMacLine));
  const events = [];
  for (let size = 1; left > 0; size = Math.min(kMaxMacLines, size + 1 + Math.floor(Math.random() * 3))) {
    const lines = Math.min(size, left);
    events.push(Math.sign(delta) * lines);
    left -= lines;
  }
  return events;
}

/**
 * @param at {x, y} where the wheel turns, browser-relative.
 * @param delta {deltaX, deltaY, deltaZ} as Page.dispatchWheelEvent got them.
 * @param macOS whether the identity is macOS.
 * @returns the wheel events, each due `t` ms after the first.
 */
export function wheelNotches(at, {deltaX, deltaY, deltaZ}, macOS) {
  const eventsFor = macOS ? toMacLines : toNotches;
  const xs = eventsFor(deltaX);
  const ys = eventsFor(deltaY);
  const count = Math.max(1, xs.length, ys.length);
  const steps = [];
  let t = 0;
  for (let i = 0; i < count; i++) {
    if (i)
      t += 18 + Math.random() * 42;
    const dx = xs[i] ?? 0;
    const dy = ys[i] ?? 0;
    steps.push({
      t, x: at.x, y: at.y,
      dx, dy, dz: i ? 0 : deltaZ,
      mode: 1 /* WheelEvent.DOM_DELTA_LINE */,
      ticks: {x: dx, y: dy},
    });
  }
  return steps;
}
