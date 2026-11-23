# Review window: PR changes

Guild's **Review → open** opens a PR's review worktree as a `review-*.code-workspace` window in VS Code. This small
local extension makes that window show the GitHub Pull Requests side bar, so the PR's own changed-files tree (only the
added, changed and deleted files) is in view at once, instead of the Explorer, and opens the PR's changes: the
"Changes in Pull Request #N" view, every changed file's diff in one tab.

It does nothing in any other window, and it changes no settings.

## Install (and after a VS Code update, if it is gone)

```sh
cd vscode/guild-review-pr-tree
npx -y @vscode/vsce@3 package --allow-missing-repository --skip-license -o guild-review-pr-tree.vsix
code --install-extension guild-review-pr-tree.vsix --force
```

Then restart VS Code once. It needs the GitHub Pull Requests extension (`GitHub.vscode-pull-request-github`).

## How it works

On a `review-*.code-workspace` window it runs the GitHub Pull Requests extension's command that opens its side bar,
and once more five seconds later: the "Changes In Pull Request" view appears only after the PR is fetched.

It then runs that extension's **Open Changes** command for the PR named in the workspace's `guild.reviewPullRequest`
setting, which Guild writes. The extension opens a PR's changes by itself only right after its own checkout, never
when a folder opens already on the PR's branch, so a review window needs this. The command gets the PR's number
alone, so it looks the PR up through this window's own repository: until the extension has read the folder's GitHub
remote the command fails, and it is tried every two seconds for up to two minutes.
