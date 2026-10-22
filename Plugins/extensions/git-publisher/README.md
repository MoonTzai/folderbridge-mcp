# Git Publisher

Pure external / hot-updatable FolderBridge Extension for GitHub publishing, including first publication. It is **never bundled into the main executable**:

1. inspect the selected workspace-root repository or one explicit nested Git repository selected by `repo_path`, or initialize a new one;
2. connect `github.com` through Git Credential Manager's browser OAuth flow;
3. commit only an explicit allowlist of regular workspace files;
4. push only the current named branch to the existing credential-free `https://github.com/<owner>/<repo>[.git]` origin;
5. publish explicit files from any selected GitHub workspace repository through the generic `release-assets` action.

## First-time repository publication (1.6.0)

Once installed from the plugin ZIP and its exact hash + declared permissions are approved in **Extensions & Skills**, all actions—including read-only status—can be used. Future plugin updates use the same local ZIP installer and exact-hash reapproval; the main application does not need to be rebuilt. The first migration from pre-externalization binaries requires the one-time core build with the bundled Git Publisher removed.

New bounded actions allow first publication without a one-off Node task or a
second GitHub CLI login. GitHub repository creation is explicitly declared as
the additional `github.create-repository` capability.

1. `first-publication-preflight(repo_path)`: read-only inspection of an
   existing selected workspace directory, its local repository state and
   Git Credential Manager availability; never makes a repository.
2. `init-local-repository(repo_path)`: initialize **only** the existing
   selected regular directory as a new `main` Git root. An internal recovery
   journal lives in `.git` and is never uploaded.
3. `create-github-repository(repo_path, owner, name, visibility, description)`:
   require an explicit `public` or `private` choice; create **only on the
   currently authenticated personal account**, with Git Credential Manager's
   already-approved OAuth. No organization repositories, implicit public
   defaults, remote takeover, or tokens in tool arguments. The remote identity
   is verified before adding a credential-free HTTPS origin.
4. `initial-commit(repo_path, paths, message)`: stage only explicit files,
   reject credential-like paths and unrelated staged files, and ensure every
   staged Git Blob matches its source bytes (preventing silent CRLF/LF
   normalization). Exact first-commit retries retain the original paths/message.
5. Existing `push(repo_path)`: only push the current branch without force and
   verify remote HEAD SHA. The ordinary existing `commit` remains unchanged.

A client can call these **separate** actions sequentially after user
authorization, without requesting extra UI clicks per step or registering a
new Node task for each repository. Build/test commands still require their
own independently approved execution route.

If the user's GCM OAuth grant lacks repository-creation permissions, the
action fails with a precise authorization error. After local reauthorization,
the existing request can be retried. A remote that was already present
cannot be silently adopted. If the POST response might have been lost after
remote creation, the recovery state reports ambiguity and fails closed for
manual reconciliation; it never force pushes or overwrites remote history.

This README describes *source code for plugin version 1.6.0*. A running
FolderBridge installation must actually bundle and expose version 1.6.0 before the
upgrade can be considered installed or accepted.

## Repository selection

Existing-repository actions accept optional `repo_path`, defaulting to `.`. All four first-publication actions require an **explicitly provided** `repo_path` (including `.` only when the entire selected workspace is intended). The value is a trimmed POSIX-style path relative to the selected FolderBridge workspace. This lets one FolderBridge project workspace contain an independently published Git repository such as `Debate-Judge-Public/` without registering an overlapping child workspace.

Repository selection is fail-closed: absolute paths, `..`, backslashes, VCS/dependency containers, symlinks, junctions/reparse points, missing directories, and paths that resolve outside the selected workspace are rejected. After selection, `git rev-parse --show-toplevel` must resolve **exactly** to that selected directory; pointing `repo_path` at an ordinary subdirectory inside a larger Git repository is rejected.

When `repo_path` is omitted, Git Publisher 1.6.0 preserves the previous contract for existing repositories: the selected FolderBridge workspace itself must be the Git repository root.

For `commit`, each `paths[]` entry is relative to the selected repository root. For `release-assets`, each `assets[].path` is likewise relative to that repository root. Files elsewhere in the same FolderBridge workspace are outside that Git action's path namespace.

## Authentication

`connect` invokes the Git Credential Manager installed with Git for Windows:

```text
git credential-manager github login --url https://github.com --web
```

Git Credential Manager opens GitHub in the user's browser and stores the resulting credential in Windows Credential Manager. The FolderBridge action does not accept a token parameter, does not put credentials in `.folderbridge.json`, does not rewrite the Git remote, and redacts GitHub token-shaped strings from captured subprocess output.

If browser OAuth is unavailable, a PAT may still be configured outside the MCP conversation through Git Credential Manager or another user-controlled Git setup. Git Publisher intentionally does not accept PAT text from the model/tool arguments.

## Commit safety

`commit` requires an explicit list of individual repository-relative paths. It never runs `git add .` and does not support directory commits. Existing selections must be regular workspace files; missing selections are accepted only when they are already Git-tracked and currently deleted. Credential/key-like files, generated/dependency/VCS directories, missing untracked paths, unrelated pre-existing staged changes, and selected files with content-transforming Git attributes are rejected. Existing files are staged normally, validated tracked deletions are removed from the index explicitly, Git hooks and commit signing are disabled for the bounded commit, and staged-set verification uses `--no-renames` so Git's rename detection cannot collapse a delete+add migration and falsely report a missing allowlist path.

## Push safety

`push` re-validates that:

- the selected `repo_path` itself is the repository root;
- HEAD is on a normal named branch;
- `origin` is a credential-free GitHub HTTPS URL;
- unsafe repository-local credential helpers, URL rewrites, push URLs, hooks, fsmonitor commands, external diffs, or filter commands are absent.

The command pushes only `HEAD` to the same current branch, uses `--no-verify`, never force-pushes, disables interactive terminal prompts, and forces Git Credential Manager as the credential helper for the push.

## Release assets

Git Publisher 1.6.0 has no project-specific release action. Repository-specific version rules, build steps, commit-title conventions, or fixed asset names belong to that repository's own workflow/task layer.

`release-assets` publishes an explicit allowlist of regular files from the **selected repository only**, where the repository is the workspace root by default or an explicitly validated nested Git repository selected by `repo_path`. Its public inputs are:

- `tag`: a bounded valid Git tag name;
- `title`: one bounded non-empty title line;
- `assets`: 1..64 objects with required repository-relative `path`, optional GitHub-stable ASCII download `name`, and optional one-line display `label`;
- `latest`: boolean, default `true`. `true` explicitly marks the Release as Latest; `false` explicitly passes `--latest=false`.

Before any remote mutation, the action validates the GitHub HTTPS repository, verifies every asset path and SHA-256, obtains Git Credential Manager credentials, and snapshots every asset into a plugin-owned temporary directory. The snapshot can receive a stable download filename such as `App-Windows-v1.2.3.exe` without renaming the repository file, while a separate label such as `Windows版` is passed to GitHub for user-facing display.

Publishing then requires the tracked worktree to be clean, no staged changes, and `origin/<current-branch>` to equal current `HEAD`. Explicit untracked build artifacts are allowed as Release assets. A requested tag may be absent or already point to current `HEAD`; a local or remote tag pointing elsewhere is rejected. Missing tags are created and pushed without force.

The action creates or updates the matching GitHub Release, uses `--clobber` only for the explicitly named uploaded assets, and never deletes unspecified existing Release assets. After upload it re-reads the Release and verifies the tag, URL, each requested asset name and byte size, verifies any requested display label, verifies the requested `Latest` state through the repository `latestRelease` pointer, then re-verifies that the remote tag still points to the original `HEAD`. Release asset contents are returned with their pre-upload SHA-256 values for auditability.

Credential/key-like source paths, VCS/dependency paths, links/reparse points, traversal, GitHub-unstable/non-ASCII Release filenames, duplicates, and files at or above 2 GiB are rejected. The action runs as a FolderBridge Job with a two-hour timeout so long uploads can be polled or cancelled without holding one foreground request open; no token parameter is exposed through MCP and credentials remain in Windows Credential Manager except for the bounded child `gh.exe` environment.
