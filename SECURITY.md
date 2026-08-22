# Security Policy

## Supported versions

FolderBridge MCP is currently in early public beta. Security fixes are applied to the latest release and the `main` branch.

## Reporting a vulnerability

Please use GitHub's **Report a vulnerability** form in the repository Security tab. Do not open a public issue for an unpatched vulnerability, and do not include API keys, credentials, private source code, or personal data in a report.

Include the affected version, operating system, a minimal reproduction, the expected security boundary, and the observed result. Reports made in good faith will be acknowledged as soon as practical.

## Scope reminders

FolderBridge reduces MCP and filesystem attack surface, but it is not an OS sandbox. In particular, locally approved tasks execute repository code with the current user's permissions. Read [docs/security-model.md](docs/security-model.md) before exposing sensitive workspaces.
