---
name: code-review
description: Review a bounded diff against repository standards and the originating specification.
---

# Code Review — FolderBridge Adaptation

Review a bounded diff from a fixed point and keep two questions independent:

1. **Standards** — does the changed code fit the repository's documented conventions, architecture, safety rules, and high-signal maintainability expectations?
2. **Spec** — does the implementation actually satisfy the originating request/design/spec, including negative requirements and promised behavior?

Establish the fixed point and read the relevant diff before judging. Read repository standards and the source spec where available; if one axis lacks evidence, say so instead of inventing requirements.

Report concrete findings with severity, affected location, evidence, consequence, and a focused remediation. Distinguish correctness/security defects from maintainability smells. Avoid style-only churn unless the repository explicitly requires it. Check for duplication, knowledge leaking across seams, speculative abstractions, message chains, feature envy, and changes that make one concept require edits in many distant modules.

Finish with the smallest set of findings that materially changes confidence in the implementation. A clean review means no supported finding remains, not that every file received a comment.
