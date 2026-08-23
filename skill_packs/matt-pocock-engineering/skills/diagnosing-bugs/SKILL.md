---
name: diagnosing-bugs
description: Diagnose bugs with a reproducible red signal, minimized case, ranked hypotheses, and regression test.
---

# Diagnosing Bugs — FolderBridge Adaptation

For a hard bug, the first deliverable is a tight feedback loop that goes **red on the user's actual symptom**. Prefer a deterministic failing unit/integration/e2e test; otherwise use a minimal CLI harness, replay, differential check, or repeated stress loop. Do not build a theory from code reading before you can reproduce the failure.

Minimize the repro until unrelated variables are removed. Then generate several ranked, falsifiable hypotheses. Each hypothesis must predict what one targeted probe or controlled change would do. Instrument only at boundaries that distinguish those hypotheses; change one variable at a time.

Fix the smallest cause that explains the red signal, not nearby symptoms. Run the original repro first, then the relevant local tests, then the full regression suite. Keep a regression test that would have failed before the fix.

If the bug cannot be locked down because there is no stable test seam, treat that as an architecture finding and hand the seam problem to codebase-design / improve-codebase-architecture rather than hiding it with more logging or duplicated checks.
