import assert from "node:assert/strict";
import test from "node:test";

import { formatMoney, formatPercent, formatPrice, sideLabel } from "../../display/src/lib/closed-loop.ts";

test("closed-loop values are formatted for a Chinese investment report", () => {
  assert.equal(formatMoney("97762.92704000"), "¥97,762.93");
  assert.equal(formatPrice("12.35233999"), "¥12.35");
  assert.equal(formatPercent(0.61), "61.0%");
  assert.equal(sideLabel("buy"), "买入");
  assert.equal(sideLabel("sell"), "卖出");
});
