import assert from "node:assert/strict";
import { test } from "node:test";

import { renderGreeting } from "./index";

test("renders a greeting", () => {
  assert.equal(renderGreeting("Ada"), "Hello, Ada!");
});
