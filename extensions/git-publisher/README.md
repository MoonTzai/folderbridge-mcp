# Git Publisher

Bundled FolderBridge extension for a narrow GitHub publish workflow:

1. inspect the selected Git repository;
2. connect `github.com` through Git Credential Manager's browser OAuth flow;
3. commit only an explicit allowlist of regular workspace files;
4. push only the current named branch to the existing credential-free `https://github.com/<owner>/<repo>[.git]` origin;
5. keep the legacy `release` action locked to FolderBridge's own versioned Windows release;
6. publish explicit files from any selected GitHub workspace repository through the generic `release-assets` action.

## Authentication

`connect` invokes the Git Credential Manager installed with Git for Windows:

```text
git credential-manager github login --url https://github.com --web
```

Git Credential Manager opens GitHub in the user's browser and stores the resulting credential in Windows Credential Manager. The FolderBridge action does not accept a token parameter, does not put credentials in `.folderbridge.json`, does not rewrite the Git remote, and redacts GitHub token-shaped strings from captured subprocess output.

If browser OAuth is unavailable, a PAT may still be configured outside the MCP conversation through Git Credential Manager or another user-controlled Git setup. Git Publisher intentionally does not accept PAT text from the model/tool arguments.

## Commit safety

`commit` requires an explicit list of individual workspace paths. It never runs `git add .` and does not support directory commits. Existing selections must be regular workspace files; missing selections are accepted only when they are already Git-tracked and currently deleted. Credential/key-like files, generated/dependency/VCS directories, missing untracked paths, unrelated pre-existing staged changes, and selected files with content-transforming Git attributes are rejected. Existing files are staged normally, validated tracked deletions are removed from the index explicitly, Git hooks and commit signing are disabled for the bounded commit, and staged-set verification uses `--no-renames` so Git's rename detection cannot collapse a delete+add migration and falsely report a missing allowlist path.

## Push safety

`push` re-validates that:

- the FolderBridge workspace itself is the repository root;
- HEAD is on a normal named branch;
- `origin` is a credential-free GitHub HTTPS URL;
- unsafe repository-local credential helpers, URL rewrites, push URLs, hooks, fsmonitor commands, external diffs, or filter commands are absent.

The command pushes only `HEAD` to the same current branch, uses `--no-verify`, never force-pushes, disables interactive terminal prompts, and forces Git Credential Manager as the credential helper for the push.

## Release actions

`release` remains the compatibility-locked FolderBridge release path. It still derives the version from FolderBridge's `pyproject.toml`, requires the expected FolderBridge release commit, and publishes only the fixed FolderBridge Windows assets. The generic path does not weaken or replace those checks.

`release-assets` publishes an explicit allowlist of regular files from the **selected workspace repository only**. Its public inputs are:

- `tag`: a bounded valid Git tag name;
- `title`: one bounded non-empty title line;
- `assets`: 1..64 objects with required workspace-relative `path`, optional GitHub-stable ASCII download `name`, and optional one-line display `label`;
- `latest`: boolean, default `true`. `true` explicitly marks the Release as Latest; `false` explicitly passes `--latest=false`.

Before any remote mutation, the action validates the GitHub HTTPS repository, verifies every asset path and SHA-256, obtains Git Credential Manager credentials, and snapshots every asset into a plugin-owned temporary directory. The snapshot can receive a stable download filename such as `App-Windows-v1.2.3.exe` without renaming the repository file, while a separate label such as `Windows版` is passed to GitHub for user-facing display.

Publishing then requires the tracked worktree to be clean, no staged changes, and `origin/<current-branch>` to equal current `HEAD`. Explicit untracked build artifacts are allowed as Release assets. A requested tag may be absent or already point to current `HEAD`; a local or remote tag pointing elsewhere is rejected. Missing tags are created and pushed without force.

The action creates or updates the matching GitHub Release, uses `--clobber` only for the explicitly named uploaded assets, and never deletes unspecified existing Release assets. After upload it re-reads the Release and verifies the tag, URL, each requested asset name and byte size, verifies any requested display label, verifies the requested `Latest` state through the repository `latestRelease` pointer, then re-verifies that the remote tag still points to the original `HEAD`. Release asset contents are returned with their pre-upload SHA-256 values for auditability.

Credential/key-like source paths, VCS/dependency paths, links/reparse points, traversal, GitHub-unstable/non-ASCII Release filenames, duplicates, and files at or above 2 GiB are rejected. The action runs as a FolderBridge Job with a two-hour timeout so long uploads can be polled or cancelled without holding one foreground request open; no token parameter is exposed through MCP and credentials remain in Windows Credential Manager except for the bounded child `gh.exe` environment.
