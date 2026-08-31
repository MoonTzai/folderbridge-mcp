# FolderBridge External Skill Packs

This directory is reserved for **public, optional, non-bundled Skill Pack source** distributed with the repository.

Keep the repository boundaries distinct:

- `skill_packs/` contains Skill Packs that are intentionally bundled into FolderBridge release builds through the explicit packaging allowlist.
- `Plugins/skill-packs/` contains optional public external Skill Packs that users may install into their per-user FolderBridge Skill Pack directory and approve by exact hash.
- `local-private/skill-packs/` is the preferred ignored workspace location for new local-only/private Skill Packs. Existing explicitly ignored private packs may remain in an operationally convenient location; they must never be copied into public source merely to make Git status or repository audits look complete.

A Skill Pack belongs here only when its source, documentation, licensing, and redistribution status are intentionally public. Do not mirror bundled packs here: one component should have one canonical repository source role.
