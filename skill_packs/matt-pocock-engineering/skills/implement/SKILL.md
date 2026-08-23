---
name: implement
description: Implement an agreed specification through existing seams with tests, checks, and review.
---

# Implement — FolderBridge Adaptation

Use this only after the requested behavior and important seams are already settled. Do not reopen product/design questions without evidence that the agreed design is impossible or unsafe.

Execute the plan in small vertical slices. Use TDD where the behavior has a stable observable seam. Run focused tests frequently and the full regression suite before declaring completion. Keep type/static checks in the loop when the project has them.

Track deviations from the spec explicitly: either correct the implementation or update the design with a reason. Do not silently broaden scope.

When implementation is green, perform a code-review pass against both repository standards and the originating design/spec. Resolve supported high/medium findings, rerun the suite, and leave the worktree/diff in an auditable state. Version/build/publish actions are separate explicit steps unless the user's request already includes them.
