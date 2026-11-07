import { template } from "lodash";

// lodash 4.17.20 has a command injection hole in `template` (fixed in 4.17.21).
// This call makes the vulnerable function reachable, which later milestones need to detect.
// The template text is fixed here, so this sample itself can't be exploited.
export function renderGreeting(name: string): string {
  return template("Hello, <%= name %>!")({ name });
}
