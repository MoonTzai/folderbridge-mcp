# Contributing

Thanks for helping improve FolderBridge MCP.

1. Open an issue for substantial behavior or security-boundary changes before implementation.
2. Keep the runtime dependency-free unless a dependency clearly reduces total risk and maintenance burden.
3. Preserve the one-workspace boundary, bounded inputs/outputs, `shell=False`, and explicit opt-in for write or task capabilities.
4. Add or update tests for every behavior change.
5. Run `python -m unittest discover -s tests -v` before opening a pull request.

Windows release builds use the exact PyInstaller version in `requirements-build.txt` and `scripts/build_windows.ps1`. Do not commit generated `.build`, `.build-venv`, `release`, or executable files; release binaries belong in GitHub release assets.

Do not include secrets, private repositories, generated credentials, or machine-specific launcher settings in commits. Security vulnerabilities should follow [SECURITY.md](SECURITY.md), not the public issue tracker.

By submitting a contribution, you agree that it is licensed under the Apache License 2.0.
