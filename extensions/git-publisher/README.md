# Git Publisher

Bundled FolderBridge extension for a narrow GitHub publish workflow:

1. inspect the selected Git repository;
2. connect `github.com` through Git Credential Manager's browser OAuth flow;
3. commit only an explicit allowlist of regular workspace files;
4. push only the current named branch to the existing credential-free `https://github.com/<owner>/<repo>[.git]` origin.

## Authentication

`connect` invokes the Git Credential Manager installed with Git for Windows:

```text
git credential-manager github login --url https://github.com --web
```

Git Credential Manager opens GitHub in the user's browser and stores the resulting credential in Windows Credential Manager. The FolderBridge action does not accept a token parameter, does not put credentials in `.folderbridge.json`, does not rewrite the Git remote, and redacts GitHub token-shaped strings from captured subprocess output.

If browser OAuth is unavailable, a PAT may still be configured outside the MCP conversation through Git Credential Manager or another user-controlled Git setup. Git Publisher intentionally does not accept PAT text from the model/tool arguments.

## Commit safety

`commit` requires an explicit list of individual files. It never runs `git add .`, does not support directory commits or deletions, rejects credential/key-like files and generated/dependency/VCS directories, refuses to operate when unrelated staged changes already exist, rejects selected files with content-transforming Git attributes, disables Git hooks and commit signing for the bounded commit, and verifies the staged set exactly matches the requested allowlist before committing.

## Push safety

`push` re-validates that:

- the FolderBridge workspace itself is the repository root;
- HEAD is on a normal named branch;
- `origin` is a credential-free GitHub HTTPS URL;
- unsafe repository-local credential helpers, URL rewrites, push URLs, hooks, fsmonitor commands, external diffs, or filter commands are absent.

The command pushes only `HEAD` to the same current branch, uses `--no-verify`, never force-pushes, disables interactive terminal prompts, and forces Git Credential Manager as the credential helper for the push.
