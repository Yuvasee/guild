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

It then opens the PR's changes itself: the worktree's HEAD against its merge base with `origin/<base>` (the base
branch from the workspace's `guild.reviewBase`, fetched first), in one "Changes in Pull Request #N at <commit>" tab.
Each time HEAD moves (bridge fast-forwards the review branch when the PR gets new commits) it builds the view again and
closes the older one. The GitHub Pull Requests extension's own changes view keeps the file list it fetched first, and
its refresh does not refetch it, so after a push it went on showing the old diff.
