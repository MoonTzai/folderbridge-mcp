# FolderBridge Skill Engine — Design and Code Plan

Status: implemented; audit converged
Target release: 0.7.0
Date: 2026-08-23

## 1. Goal

Add a general, local-first Skill Engine to FolderBridge so ChatGPT/web clients can discover, match, and load trusted methodology Skills on demand without adding MCP tool names for every Skill.

A focused engineering-methods Pack is the first bundled Skill Pack, not a hard-coded special case. The engine remains general for future bundled and user-custom packs.

The feature has two separate jobs:

1. **Skill registry/runtime** — safely discover, trust, match, and return bounded Skill text.
2. **Model routing hint** — make the existing MCP connection tell the model when/how to ask the Skill Engine for a relevant Skill.

FolderBridge cannot force the ChatGPT model to invoke a tool when the model never selects the MCP app. The design therefore maximizes automatic selection through stable MCP instructions and a stable `extension` adapter, while keeping the actual routing logic server-side and hot-reloadable.

## 2. Design vocabulary and constraints

This design uses a deep-module vocabulary centered on module depth, interfaces, seams, adapters, leverage, and locality:

- **Module**: `SkillEngine` owns Skill discovery, trust, matching, content loading, and provenance.
- **Interface**: model-facing callers use `describe()` (trusted/enabled view only), `match(task)`, `get(skill_ref, expected_sha256, resource=None)`, and `routing_index()`; the local launcher uses `describe(include_untrusted=True)` plus explicit `approve_pack(pack_id, expected_sha256)`, `set_enabled(pack_id, enabled)`, and `revoke_pack(pack_id)` mutations. The trust store itself is not exposed to callers.
- **Seam**: the `SkillEngine` interface sits between local Skill Pack files and both callers that need Skill data.
- **Adapters**: two real adapters use the seam:
  1. `ToolRuntime.instructions` consumes a compact routing index.
  2. bundled `skill-engine` Extension exposes `list`, `match`, and `get` through the existing stable MCP `extension` tool.
- **Depth**: pack scanning, bounds, exact hashes, trust rules, lexical matching, provenance, and content validation stay behind the small interface.
- **Locality**: changes to Skill format/routing/trust live in `skills.py`, not in GUI, tools, or extension plugin callers.

The interface itself is the test surface. Tests should exercise public Skill Engine behavior rather than private parsing helpers wherever practical.

## 3. Alternatives considered

### A. Add a new MCP `skill` tool

Advantages:
- obvious model-facing semantics;
- compact action schema.

Rejected for v1 because:
- it changes the MCP tool catalog and can require Connector/Tunnel schema refresh;
- future Skills should not produce future MCP schema churn;
- the existing `extension` gateway already exists specifically to keep the catalog stable.

### B. Put all Skill logic directly inside `extension`

Advantages:
- no new module.

Rejected because:
- Extension semantics are executable Python + permissions + worker lifecycle;
- Skill semantics are trusted model instructions + provenance + bounded text;
- mixing them would make one shallow module own two different trust models.

### C. Core Skill Engine + thin bundled Extension adapter

Chosen.

The core module is independent from Extension execution. A tiny bundled `skill-engine` Extension imports the packaged core module and adapts `list/match/get` to the existing gateway. `ToolRuntime` uses the same core module directly for initialization instructions. Two callers justify a real seam.

## 4. Trust model

Skills do **not** execute local code, but Skill text can change model behavior. Therefore external Skill Packs are not automatically trusted.

### Bundled packs

- shipped inside FolderBridge;
- trusted as part of the FolderBridge release; they never need a user hash approval record;
- enabled by default unless the user locally disables the pack;
- a local disable is a persistent preference override independent from pack hash/version, so upgrading FolderBridge does not silently re-enable a pack the user turned off;
- retain provenance metadata and hashes for auditability.

### User packs

- hot-scanned from the per-user Skill Pack directory;
- exact pack-tree SHA-256 must be locally approved;
- approval is an optimistic-concurrency operation: `approve_pack()` requires the exact `expected_sha256` that the launcher displayed, rescans the pack, and refuses if it changed before approval is persisted;
- any later file change makes approval stale;
- disabled until approved/enabled;
- launcher approval dialog explicitly states that the pack is prompt/methodology content, not executable code, but can influence model behavior.

Untrusted pack metadata is **administrative data only**. The local launcher may inspect it to ask for approval, but model-facing `describe/match/get/routing_index` never return untrusted pack names, descriptions, routing terms, or Skill text.

No Skill Pack receives filesystem, process, environment, or network permissions. Skill Engine never executes a Skill file.

Trust store v1 keeps two concepts separate:

```json
{
  "version": 1,
  "external": {"pack-id": {"sha256": "...", "enabled": true}},
  "bundled_disabled": ["pack-id"]
}
```

External approval is hash-bound. Bundled disable preference is intentionally not hash-bound. `revoke_pack()` applies only to external packs; bundled packs are controlled by `set_enabled()`.

## 5. Pack format

Directory:

```text
<pack-id>/
  folderbridge-skill-pack.json
  LICENSE                  # optional but recommended
  skills/
    <skill-id>/
      SKILL.md
      ... declared resources ...
```

The JSON manifest is authoritative. FolderBridge deliberately does not depend on YAML frontmatter parsing for registration/routing. Original `SKILL.md` frontmatter remains untouched and is returned as part of the Skill text.

Manifest v1:

```json
{
  "schema_version": 1,
  "id": "folderbridge-engineering",
  "name": "FolderBridge Engineering Methods",
  "version": "1.0.0",
  "description": "Curated engineering methodology Skills",
  "source": {},
  "skills": [
    {
      "id": "codebase-design",
      "name": "Codebase Design",
      "path": "skills/codebase-design/SKILL.md",
      "description": "Shared vocabulary for deep-module design.",
      "routing_terms": ["architecture", "module", "interface", "seam", "架构", "模块", "接口"],
      "resources": []
    }
  ]
}
```

### Bounds

Initial limits:

- manifest: 256 KiB;
- one Skill/resource text file: 128 KiB;
- one pack: 128 files / 4 MiB hash-covered bytes;
- at most 32 discovered packs per root;
- 128 Skills per pack;
- routing term: 1..120 characters;
- at most 64 routing terms per Skill;
- returned Skill text is exact UTF-8 and bounded by the file limit.

Links/reparse points, traversal, duplicate qualified Skill IDs, unknown fields, malformed strict JSON, and non-UTF-8 content are rejected. A user pack may not shadow a bundled pack ID; the bundled pack wins and the user directory is reported as a scan error. Pack hashing covers every accepted regular file in the pack tree, not only declared Skill paths, so adding undeclared content also invalidates an external approval.

## 6. Identity and provenance

Qualified Skill reference:

```text
<pack-id>/<skill-id>
```

Every listed/matched Skill exposes:

- qualified `skill_ref`;
- pack id/version;
- pack SHA-256;
- Skill file SHA-256;
- bundled/trusted/enabled state;
- source repository/ref/commit/license when declared;
- description and routing metadata.

Model-facing `get()` requires `expected_sha256`, and automatic routing passes the Skill hash returned by `match()` so a changed user Skill cannot silently become a different instruction between match and load. `get()` reads the selected document bytes, computes SHA-256 over exactly those returned bytes, and compares that digest to both the current record and `expected_sha256` before decoding/returning them. The optional `resource` parameter may load only a resource path explicitly declared for that Skill; resources are individually hashed and bounded by the same text limit, and their hashes are returned with the main Skill metadata.

## 7. Matching

Matching is deterministic and local. v1 does not add embeddings, remote inference, or network calls.

Input:

```python
SkillEngine.match(task: str, *, limit: int = 3)
```

Rules:

1. only enabled/trusted Skills participate;
2. normalize task with Unicode casefold plus collapsed whitespace;
3. ASCII single-word terms match on word boundaries; multi-word phrases match normalized substrings; CJK/non-ASCII terms match normalized substrings;
4. every distinct matched term contributes a deterministic score, with multi-word/longer terms weighted above short generic terms;
5. de-duplicate matched terms and use qualified Skill ref only as the final stable tie-breaker;
6. return at most 3 by default / 5 maximum;
7. if score is zero for all Skills, return no match rather than forcing a method.

The matcher returns evidence (`matched_terms`, score) so routing behavior is inspectable and testable.

The bundled engineering manifest includes English and Chinese routing terms for the engineering intents FolderBridge most often sees.

## 8. Bundled engineering Pack v1 scope

The first release ships a concise, self-contained engineering-methods Pack written for FolderBridge's tool model. The Pack is ordinary trusted methodology text and does not receive executable permissions.

Users may separately install other compatible Skill Packs in the user Skill directory; the same Engine will hash, trust, route, and load them without code changes.

Bundle these six methods first because they compose into the requested architecture → implementation → diagnosis/review loop and are useful without taking over project management:

- `codebase-design`
- `improve-codebase-architecture`
- `diagnosing-bugs`
- `tdd`
- `code-review`
- `implement`

The v1 methods are self-contained and declare no supplemental resources. Future compatible Packs can add more methods without changing MCP tool names or widening the Skill Engine interface.

## 9. MCP/web routing

No new MCP tool is introduced.

Bundled Extension:

```text
extensions/skill-engine/
  folderbridge-extension.json
  plugin.py
```

It is bundled, read-only, requires no workspace, has no permissions, and uses `authorization=none`.

Actions:

- `list` — return enabled/trusted Skill metadata and a sanitized scan-error count/code summary; raw untrusted paths/descriptions remain local to the launcher admin view;
- `match` — params `{task, limit?}`;
- `get` — params `{skill_ref, expected_sha256, resource?}`; stale/missing hashes fail closed. The adapter places the verified Skill/resource body in MCP `_content` text and keeps provenance/hash metadata in structured content, so ordinary JSON preview truncation cannot silently replace the method body.

`plugin.py` must be a thin adapter only; matching/parsing/trust logic stays in `folderbridge_mcp.skills`.

### Shared user-path seam

`SkillEngine` runs both in the MCP host and inside the bundled Extension worker. FolderBridge therefore adds one shared `user_config_root()` module/function rather than letting each caller infer profile paths independently.

When the host starts an Extension worker it injects a reserved internal `FOLDERBRIDGE_CONFIG_ROOT` value computed from that shared function. The value is not user-inheritable and is not a credential; it guarantees workers use the same FolderBridge profile root even when the cleaned worker environment intentionally omits `LOCALAPPDATA` / `XDG_CONFIG_HOME`.

Existing launcher/Extension config-root helpers should be migrated to this same seam where doing so is behavior-preserving. Project-config trust state may continue to use its intentionally separate XDG state directory.

### Runtime instructions

`ToolRuntime.instructions` appends a bounded Skill routing paragraph, approximately:

> FolderBridge has a local Skill Engine available through the bundled `skill-engine` Extension. For software architecture, module/interface design, debugging, test-first implementation, or code-review tasks, call `extension(run, extension_id="skill-engine", extension_action="match", params={"task": ...})` before acting; then load recommended content with `get`. If there is no match, continue normally. Skills are methodology text, not executable tools.

A compact current routing index can follow, but total Skill-related instruction text must stay under 4 KiB. Never inject full Skill bodies into initialization instructions.

New user packs are still hot-loadable after connection because the stable instruction tells the model to call `match`; the server-side matcher sees the current filesystem even when initialization instructions are cached.

## 10. Launcher UI

Keep one default-collapsed right sidebar, but rename its conceptual scope to **Extensions & Skills**.

Within the existing scroll area:

1. Extensions section — existing behavior unchanged.
2. Skill Packs section:
   - pack checkbox;
   - name/version;
   - bundled / trusted / stale / enabled status;
   - Skill count;
   - source/ref/license summary;
   - details button;
   - revoke approval for external packs;
   - `Skill 目录` button beside `插件目录`.

External-pack enable flow:

1. read current exact pack hash;
2. display pack name/version/hash/source and Skill list;
3. warn that Skill text can influence model behavior but cannot execute local code;
4. approve exact hash and enable.

The bundled engineering Pack starts enabled and may be disabled locally without revoking FolderBridge itself.

## 11. CLI and packaging

Add:

```text
FolderBridge skills --json
```

Purpose: diagnostics and packaged smoke testing, not a new MCP surface.

Windows build:

- add `skill_packs` to PyInstaller data;
- smoke must find bundled `skill-engine` Extension;
- `FolderBridge skills --json` must find the bundled engineering Pack and expected core Skills;
- existing extension worker smoke remains.

## 12. File-level code plan

### New

- `folderbridge_mcp/skills.py`
  - dataclasses: `SkillSource`, `SkillRecord`, `SkillPackRecord`, `SkillMatch`;
  - private `SkillTrustStore` owned by `SkillEngine`;
  - `SkillEngine` public interface described above;
  - strict pack parser, bounded tree hash, text/resource loader, matcher.

- `folderbridge_mcp/user_paths.py`
  - canonical per-user FolderBridge config root;
  - reserved worker config-root environment injection/reading contract.

- `extensions/skill-engine/folderbridge-extension.json`
- `extensions/skill-engine/plugin.py`
- bundled engineering Pack manifest/notes
- bundled engineering methodology Skill files
- `tests/test_skills.py`
- `tests/test_skill_engine_extension.py`

### Modify

- `folderbridge_mcp/tools.py`
  - instantiate `SkillEngine`;
  - append bounded routing instructions;
  - report Skill Pack summary in `server_info`;
  - do not add tool names.

- `folderbridge_mcp/gui.py`
  - add Skill Pack section and trust toggles to existing sidebar.

- `folderbridge_mcp/cli.py`
  - add `skills --json` diagnostic command.

- `scripts/build_windows.ps1`
  - package `skill_packs` and add smoke assertions.

- docs/version metadata/changelog/readmes.

## 13. Testing strategy

TDD vertical slices:

### Slice A — Pack parser and bounds

Red tests first:

- valid pack discovered;
- traversal/link rejected;
- strict JSON only;
- duplicate IDs and user shadowing of bundled IDs rejected;
- non-UTF-8/oversized Skill rejected;
- exact hashes stable and stale after change.

### Slice B — Trust

- bundled pack enabled by default;
- bundled disable override survives pack version/hash changes;
- external pack excluded from every model-facing view until exact-hash approval;
- approval rejects a pack changed after the displayed hash;
- file change makes approval stale;
- disabling bundled pack suppresses routing without treating it as untrusted;
- host and worker resolve the same user Skill root through the reserved config-root seam.

### Slice C — Matcher

- architecture Chinese/English tasks route to architecture/design;
- bug task routes to diagnosing-bugs;
- TDD task routes to tdd;
- unrelated task returns no match;
- deterministic tie ordering;
- disabled pack never matches.

### Slice D — Stable MCP adapter

- `skill-engine` bundled Extension actions work through `extension`;
- `get` with stale `expected_sha256` fails closed;
- `get` verifies the digest of the exact bytes it returns after the pack scan;
- MCP tool catalog before/after adding user Skill Pack remains identical;
- instructions contain routing guidance but no full Skill body and stay bounded.

### Slice E — GUI/packaging

- source regression for Skill Pack UI and warning copy;
- bundled pack data included by Windows build script;
- packaged smoke checks the engineering Pack and `skill-engine` adapter.

Full suite after every completed slice and once after final audit.

## 14. Pre-implementation design audit result

Three design-review passes were completed before implementation.

Resolved before coding:

1. host/worker config-root ownership moved to one shared seam with a reserved internal path handoff;
2. external approval now requires the exact hash displayed to the user;
3. untrusted metadata is excluded from every model-facing view;
4. bundled disable preference is separated from external hash trust;
5. `get` verifies the exact returned bytes against current and expected document hashes;
6. pack count/size are bounded so exact-hash matching has a finite worst case;
7. model-facing scan errors are sanitized while the local launcher retains actionable detail;
8. verified Skill bodies use MCP content directly rather than relying on a truncated JSON preview.

Deletion test: removing `SkillEngine` would force pack parsing, trust, routing, byte verification, and provenance policy back into at least the MCP-instructions caller, Extension adapter, and launcher, so the module has real depth rather than pass-through value.

No unresolved high/medium design issue remains. Implementation may begin under the TDD/audit loop below.

## 15. Audit loop / convergence criterion

After implementation, repeat:

1. **Architecture audit** — deletion test, interface size, duplicate ownership, cross-module knowledge, accidental pass-throughs.
2. **Code design audit** — verify callers/tests only know the declared Skill Engine interface; no matching/trust policy leaked into GUI/plugin.
3. **Bug audit** — turn every credible failure mode into a runnable red test before fixing: stale hashes, concurrent trust writes, links, malformed packs, oversized text, matcher ambiguity, packaged paths, cached routing instructions.
4. **Security/resource audit** — strict JSON, text bounds, no execution/network side effect, provenance honesty, no sensitive environment dependency.
5. **Diff/spec audit** — compare implementation to this document; mark missing requirements and scope creep separately.

Stop iterating when:

- full test suite is green;
- two consecutive audit passes find no new high/medium issue that can be reproduced or justified from the interface contract;
- remaining limitations are documented rather than hidden;
- production code has no new shell/eval/exec path;
- MCP tool-name set is unchanged.

## 16. Explicit non-goals for v1

- no automatic internet update of Skill Packs;
- no remote embeddings/vector database;
- no execution of Skill markdown;
- no arbitrary GitHub URL installer;
- no claim that MCP can force ChatGPT to invoke a tool;
- no automatic project file mutation from Skills;
- no external project-management/workflow ecosystem in the first bundled Pack.

These can be separate future adapters/features without widening the core Skill Engine interface.
