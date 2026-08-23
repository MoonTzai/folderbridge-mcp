---
name: codebase-design
description: Design deep modules with small interfaces at explicit architectural seams.
---

# Codebase Design — FolderBridge Adaptation

Use this when the problem is where a module's interface or seam should live, not merely how a function should be written.

Think in these terms: **module** is anything with an interface plus implementation; **interface** is everything callers must know, including invariants and failure modes; **depth** is how much useful behavior the implementation hides relative to interface complexity; a **seam** is a place where behavior can change without callers changing; an **adapter** translates another interface into that seam; **leverage** is benefit per fact callers must learn; **locality** keeps related decisions together.

Prefer deep modules: small stable interfaces that own substantial policy. Use the deletion test: if deleting a proposed module merely scatters the same knowledge across callers, the module was providing leverage; if deletion barely changes what callers must know, it may be shallow ceremony.

Treat the interface as the durable test surface. One adapter can justify a hypothetical seam; two independent adapters are stronger evidence the seam is real.
