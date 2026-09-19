import assert from "node:assert";

// -----------------------------------------------------------------------------
// EXACT LOGIC FROM BACKTESTPAGE.JSX
// -----------------------------------------------------------------------------

function beforeDayOwnMove(t, offset) {
  const days = t.before_days ?? [];
  const cur = days.find((d) => d.offset === offset);
  const prior = days.find((d) => d.offset === offset - 1);
  if (!cur || !prior || !prior.close) return null;
  return ((cur.close - prior.close) / prior.close) * 100;
}

function beforeWindowMove(t, span) {
  const days = t.before_days ?? [];
  const near = days.find((d) => d.offset === -1);
  const far = days.find((d) => d.offset === -Math.min(span, days.length));
  if (!near || !far || !far.close || near.offset === far.offset) return null;
  return ((near.close - far.close) / far.close) * 100;
}

function beforeMoveValue(t, bm, span) {
  return bm.mode === "single" ? beforeDayOwnMove(t, bm.offset) : beforeWindowMove(t, span);
}

function passesBeforeMove(t, bm, span) {
  if (!bm.on) return true;
  const v = beforeMoveValue(t, bm, span);
  if (v == null) return false; // never silently counted as a 0% move
  return bm.direction === "fell" ? v <= -bm.pct : v >= bm.pct;
}

// -----------------------------------------------------------------------------
// VERIFICATION TESTS
// -----------------------------------------------------------------------------

let passed = 0;
let total = 0;

function runTest() {
  console.log("Starting Before-Move verification tests...\n");

  // Synthetic data
  // -1 close = 90
  // -2 close = 100
  // -3 close = 120
  // -4 close = 100
  // -5 close = 80
  const touch = {
    id: "test",
    before_days: [
      { offset: -1, close: 90 },
      { offset: -2, close: 100 },
      { offset: -3, close: 120 },
      { offset: -4, close: 100 },
      { offset: -5, close: 80 }
    ]
  };

  function testFilter(name, t, bmConfig, span, expectedPass, expectedValue) {
    total++;
    const v = beforeMoveValue(t, bmConfig, span);
    const pass = passesBeforeMove(t, bmConfig, span);
    
    // Deep copy to verify no mutation
    const tCopy = JSON.parse(JSON.stringify(t));
    passesBeforeMove(tCopy, bmConfig, span);
    try {
      assert.deepStrictEqual(t, tCopy, "Filter mutated the touch object!");
      
      // Floating point check: allow small tolerance for expectedValue
      if (v === null) {
        assert.strictEqual(expectedValue, null, `Value mismatch: expected ${expectedValue}, got null`);
      } else {
        assert(Math.abs(v - expectedValue) < 1e-9, `Value mismatch: expected ${expectedValue}, got ${v}`);
      }
      
      assert.strictEqual(pass, expectedPass, `Pass mismatch: expected ${expectedPass}, got ${pass}`);
      passed++;
      console.log(`[PASS] ${name} (value: ${v === null ? 'null' : v.toFixed(2)}%)`);
    } catch (e) {
      console.error(`[FAIL] ${name}`);
      console.error(e.message);
    }
  }

  // A. Value clearly satisfying "fell"
  // Cumulative span=3: from -3 (120) to -1 (90) = (90-120)/120 = -25%
  testFilter("CUMULATIVE fell 10%: clearly satisfies", touch, { on: true, mode: "cumulative", direction: "fell", pct: 10 }, 3, true, -25.00);
  
  // B. Value clearly satisfying "rose"
  // Cumulative span=5: from -5 (80) to -1 (90) = (90-80)/80 = 12.5%
  testFilter("CUMULATIVE rose 10%: clearly satisfies", touch, { on: true, mode: "cumulative", direction: "rose", pct: 10 }, 5, true, 12.50);

  // C. Value exactly equal to the threshold
  // Single offset -2: cur=-2 (100), prior=-3 (120) = (100-120)/120 = -16.66666%
  // Let's create an exact scenario: -2 close 100, -3 close 110 => -9.09%. Not round.
  // We'll use a custom touch.
  const tExact = {
    before_days: [
      { offset: -1, close: 95 },
      { offset: -2, close: 100 },
      { offset: -3, close: 110 }
    ]
  };
  // Cumulative span=2: -2 (100) to -1 (95) => -5%
  testFilter("CUMULATIVE fell 5% exact: equals threshold", tExact, { on: true, mode: "cumulative", direction: "fell", pct: 5 }, 2, true, -5.00);

  // D. Value just below the threshold (does not pass)
  // Single offset -1: cur=-1 (95), prior=-2 (100) => -5%
  // Filter asks for fell 6% -> it only fell 5%, so it should FAIL.
  testFilter("SINGLE fell 6%: just below threshold", tExact, { on: true, mode: "single", offset: -1, direction: "fell", pct: 6 }, 5, false, -5.00);

  // E. Value just above the threshold (passes)
  // Filter asks for fell 4% -> it fell 5%, so it should PASS.
  testFilter("SINGLE fell 4%: just above threshold", tExact, { on: true, mode: "single", offset: -1, direction: "fell", pct: 4 }, 5, true, -5.00);

  // F. Zero movement
  const tZero = {
    before_days: [
      { offset: -1, close: 100 },
      { offset: -2, close: 100 }
    ]
  };
  testFilter("SINGLE fell 5% on 0% move", tZero, { on: true, mode: "single", offset: -1, direction: "fell", pct: 5 }, 5, false, 0.00);
  testFilter("SINGLE rose 5% on 0% move", tZero, { on: true, mode: "single", offset: -1, direction: "rose", pct: 5 }, 5, false, 0.00);

  // G. Missing before-day data
  // Asking for offset -3, but only -1 and -2 exist
  testFilter("SINGLE offset missing: cannot judge", tExact, { on: true, mode: "single", offset: -3, direction: "fell", pct: 5 }, 5, false, null);
  
  // H. No before-day data
  const tEmpty = { before_days: [] };
  testFilter("CUMULATIVE with empty data", tEmpty, { on: true, mode: "cumulative", direction: "fell", pct: 5 }, 5, false, null);

  // I. Missing prior day in single mode
  // Asking for offset -2, but -3 is missing, so we can't calculate day -2's own move
  const tNoPrior = { before_days: [{ offset: -1, close: 90 }, { offset: -2, close: 100 }] };
  testFilter("SINGLE missing prior day", tNoPrior, { on: true, mode: "single", offset: -2, direction: "fell", pct: 5 }, 5, false, null);

  // J. Disabled filter passes everything
  testFilter("DISABLED filter passes despite missing data", tEmpty, { on: false, mode: "cumulative", direction: "fell", pct: 5 }, 5, true, null);
  testFilter("DISABLED filter passes despite failing move", tExact, { on: false, mode: "cumulative", direction: "fell", pct: 100 }, 2, true, -5.00);

  // K. Cumulative uses -1 as near, and -Math.min(span, days.length) as far
  // Touch has days -1 to -5. Span=100. Should use -5 as far.
  // -5 is 80, -1 is 90 -> (90-80)/80 = 12.5%
  testFilter("CUMULATIVE span > available uses furthest available", touch, { on: true, mode: "cumulative", direction: "rose", pct: 10 }, 100, true, 12.50);

  // L. Single mode only evaluates that specific day's move
  // Day -1: 90 vs 100 = -10%. Day -2: 100 vs 120 = -16.6%.
  // Test -2 specifically.
  testFilter("SINGLE targets specific offset independent of others", touch, { on: true, mode: "single", offset: -2, direction: "fell", pct: 15 }, 5, true, -16.666666666666664);

  // Verify non-mutation manually on an array
  total++;
  const touches = [touch, tExact, tZero];
  const touchesStr = JSON.stringify(touches);
  touches.filter(t => passesBeforeMove(t, { on: true, mode: "cumulative", direction: "fell", pct: 5 }, 5));
  assert.strictEqual(JSON.stringify(touches), touchesStr, "Array mutation occurred!");
  passed++;
  console.log(`[PASS] Array non-mutation verified`);

  console.log(`\nDone. ${passed}/${total} assertions passed.`);
}

runTest();
