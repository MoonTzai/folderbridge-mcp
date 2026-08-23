---
name: tdd
description: Build one observable behavior at a time with a vertical red-green-refactor loop.
---

# Test-Driven Development — FolderBridge Adaptation

Drive one observable behavior at a time through a public interface seam.

For each vertical slice: write one test that names a behavior from the spec, run it and confirm it is red for the intended reason, implement only enough behavior to turn it green, then refactor while the suite remains green. Do not batch a large imagined test suite before implementation; each passing slice should teach you what the next slice actually needs.

Prefer tests that survive internal renames and implementation changes. Assert literal outcomes from the requirement rather than recomputing expected values with the same algorithm as production. Use a seam that callers naturally cross; if the only way to test is by reaching deep into internals, reconsider the module design first.

Run the smallest relevant test frequently and the full suite at meaningful checkpoints. Never refactor while the target behavior is still red. When a regression is being fixed, keep the red test as a permanent guard unless it only tests incidental implementation detail.
