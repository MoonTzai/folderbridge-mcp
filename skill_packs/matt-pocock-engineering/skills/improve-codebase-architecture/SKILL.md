---
name: improve-codebase-architecture
description: Survey a codebase for high-leverage opportunities to deepen shallow modules and improve locality.
---

# Improve Codebase Architecture — FolderBridge Adaptation

Use this as a focused architecture health survey, especially around code that is actively changing.

First understand the relevant modules, recent change hot spots, project vocabulary, tests, and recorded architectural decisions. Then look for **deepening opportunities**: places where callers must know too much, the same policy appears in several modules, multiple adapters imply a missing seam, or tests are forced to reach through internals because the public interface is too weak.

Do not produce a generic cleanup list. For every candidate, apply the deletion test: would removing the proposed module concentrate complexity somewhere worse, proving the module can hide real policy, or would it merely move names/files around? Prefer changes that reduce caller knowledge and improve locality/testability.

Rank candidates by leverage, current development activity, risk, and migration cost. Separate proven defects from architectural friction. Do not refactor solely because a file is large, and do not split a cohesive deep module just to reduce line count.

Once one opportunity is selected, switch to codebase-design vocabulary to design its interface before editing code.
