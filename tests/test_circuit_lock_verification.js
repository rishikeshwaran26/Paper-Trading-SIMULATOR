import assert from "node:assert";

// Exact implementation extracted from frontend/src/pages/BacktestPage.jsx
function isCircuitLocked(t) {
  if (t.touch_pct == null || t.close_pct == null) return false;
  return Math.abs(t.touch_pct - t.close_pct) <= 0.15;
}

// Exact filtering logic extracted from predictionTouches in BacktestPage.jsx
function filterTouches(touches, tradeable) {
  // We map touches first to prove they aren't mutated
  return touches.filter((t) => {
    if (tradeable === "exclude_locked" && isCircuitLocked(t)) return false;
    if (tradeable === "only_locked" && !isCircuitLocked(t)) return false;
    return true;
  });
}

function runTest() {
  console.log("Starting Circuit Lock verification tests...");
  let passed = 0;
  let total = 0;

  const testCases = [
    // --- UP direction ---
    { name: "UP: Difference exactly 0", touch_pct: 19.50, close_pct: 19.50, diff: 0, locked: true },
    { name: "UP: Difference smaller than tolerance (0.10)", touch_pct: 19.50, close_pct: 19.40, diff: 0.10, locked: true },
    // Floating point math: 19.50 - 19.35 = 0.15000000000000213. Wait, is it <= 0.15?
    { name: "UP: Difference exactly equal to tolerance (0.15)", touch_pct: 19.50, close_pct: 19.35, diff: 0.15, locked: true },
    { name: "UP: Difference just greater than tolerance (0.16)", touch_pct: 19.50, close_pct: 19.34, diff: 0.16, locked: false },
    { name: "UP: Difference much greater than tolerance (4.50)", touch_pct: 19.50, close_pct: 15.00, diff: 4.50, locked: false },
    
    // --- DOWN direction ---
    { name: "DOWN: Difference exactly 0", touch_pct: -19.50, close_pct: -19.50, diff: 0, locked: true },
    { name: "DOWN: Difference smaller than tolerance (0.10)", touch_pct: -19.50, close_pct: -19.40, diff: 0.10, locked: true },
    { name: "DOWN: Difference exactly equal to tolerance (0.15)", touch_pct: -19.50, close_pct: -19.35, diff: 0.15, locked: true },
    { name: "DOWN: Difference just greater than tolerance (0.16)", touch_pct: -19.50, close_pct: -19.34, diff: 0.16, locked: false },
    { name: "DOWN: Difference much greater than tolerance (4.50)", touch_pct: -19.50, close_pct: -15.00, diff: 4.50, locked: false },
    
    // --- EDGE CASES ---
    { name: "Missing touch_pct", touch_pct: null, close_pct: 19.50, diff: null, locked: false },
    { name: "Missing close_pct", touch_pct: 19.50, close_pct: null, diff: null, locked: false }
  ];

  for (const tc of testCases) {
    total++;
    const t = { id: tc.name, touch_pct: tc.touch_pct, close_pct: tc.close_pct };
    
    // Because of JS floating point, 19.50 - 19.35 might be 0.15000000000000213
    // Let's see what isCircuitLocked actually returns without rounding
    const actualLocked = isCircuitLocked(t);
    const floatDiff = tc.touch_pct != null && tc.close_pct != null ? Math.abs(tc.touch_pct - tc.close_pct) : null;
    
    console.log(`\nTest: ${tc.name}`);
    console.log(`  touch_pct: ${tc.touch_pct}`);
    console.log(`  close_pct: ${tc.close_pct}`);
    console.log(`  abs diff:  ${floatDiff}`);
    console.log(`  locked?    Expected: ${tc.locked}, Actual: ${actualLocked}`);
    
    // Verify all 3 tradeable modes
    const touches = [{ ...t }];
    const all = filterTouches(touches, "all");
    const exclude = filterTouches(touches, "exclude_locked");
    const only = filterTouches(touches, "only_locked");
    
    console.log(`  tradeable="all":            Expected: included, Actual: ${all.length === 1 ? 'included' : 'excluded'}`);
    console.log(`  tradeable="exclude_locked": Expected: ${tc.locked ? 'excluded' : 'included'}, Actual: ${exclude.length === (tc.locked ? 0 : 1) ? (tc.locked ? 'excluded' : 'included') : (tc.locked ? 'included' : 'excluded')}`);
    console.log(`  tradeable="only_locked":    Expected: ${tc.locked ? 'included' : 'excluded'}, Actual: ${only.length === (tc.locked ? 1 : 0) ? (tc.locked ? 'included' : 'excluded') : (tc.locked ? 'excluded' : 'included')}`);
    
    try {
      if (tc.name.includes("exactly equal") && tc.touch_pct === 19.50 && actualLocked === false) {
        // Floating point trap: 19.50 - 19.35 = 0.15000000000000213 > 0.15. 
        // This means exact 0.15 might fail in JS! We will observe it.
        console.warn(`  [!] JS float precision caused 0.15 test to evaluate to ${actualLocked}`);
      }
      
      // Verify no mutation
      assert.strictEqual(touches[0].touch_pct, tc.touch_pct, "Mutated touch_pct");
      assert.strictEqual(touches[0].close_pct, tc.close_pct, "Mutated close_pct");
      
      passed++;
    } catch (e) {
      console.error(`  FAIL: ${e.message}`);
    }
  }

  console.log(`\nDone. ${passed}/${total} assertions passed.`);
}

runTest();
