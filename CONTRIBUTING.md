# Contributing

Thanks for helping improve FolderBridge MCP.

1. Open an issue for substantial behavior or security-boundary changes before implementation.
2. Keep the runtime dependency-free unless a dependency clearly reduces total risk and maintenance burden.
3. Preserve strict per-workspace confinement and explicit workspace selection in multi-workspace mode, bounded inputs/outputs, `shell=False`, and explicit opt-in for write or task capabilities.
4. Add or update tests for every behavior change.
5. Run `python -m unittest discover -s tests -v` before opening a pull request.

Windows release builds use the exact PyInstaller version in `requirements-build.txt` and `scripts/build_windows.ps1`. Do not commit generated `.build`, `.build-venv`, `release`, or executable files; release binaries belong in GitHub release assets.

Do not include secrets, private repositories, generated credentials, or machine-specific launcher settings in commits. Security vulnerabilities should follow [SECURITY.md](SECURITY.md), not the public issue tracker.

## Public repository boundary

- Treat an explicitly designated **local-only/private Extension, Skill Pack, adapter, harness, credential bridge, or project-specific integration** as outside the public repository boundary even when it lives under the workspace for local development or runtime installation.
- An untracked local-only/private asset is **not** a synchronization omission and must never be added merely to make `git status` clean, complete an allowlist, mirror an installed runtime, or satisfy a repository-wide cleanup/audit.
- Before any broad repository synchronization, cleanup, release, or selective commit, classify untracked files into public source, generated/temporary files, and local-only/private assets. Only the public-source class is eligible for Git publication.
- Never use `git add .`, broad clean/reset, force push, or a generated-file sweep to cross that boundary. Public commits must remain explicit-path/allowlist based.
- Prefer the ignored `local-private/` tree for new repository-local private development assets. Existing explicitly ignored local-only integrations may remain in their operationally convenient locations; do not move them merely for cosmetic repository uniformity. In either case, their source, tests, documentation, credentials, logs, and runtime state stay outside the public Git boundary unless the owner explicitly reclassifies them as public.

By submitting a contribution, you agree that it is licensed under the Apache License 2.0.
