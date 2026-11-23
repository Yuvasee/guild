const vscode = require('vscode');

const PR_EXTENSION = 'GitHub.vscode-pull-request-github';
const PR_CONTAINER = 'workbench.view.extension.github-pull-request';
const PR_URL = /^https:\/\/github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)$/;
// git's own extension API (vscode.git) reports a commit-to-commit diff with these statuses.
const ADDED = new Set([1, 9]); // INDEX_ADDED, INTENT_TO_ADD
const DELETED = new Set([2, 6]); // INDEX_DELETED, DELETED

let shownHead;
let showing = false;

async function activate() {
  const workspaceFile = vscode.workspace.workspaceFile;
  if (!workspaceFile || !/\/review-[^/]*\.code-workspace$/.test(workspaceFile.path)) return;
  await vscode.extensions.getExtension(PR_EXTENSION)?.activate();
  await openPrContainer();
  // The "Changes In Pull Request" view appears only after the PR is fetched; a second open lands on it.
  setTimeout(openPrContainer, 5000);
  const git = (await vscode.extensions.getExtension('vscode.git')?.activate())?.getAPI(1);
  if (!git) return;
  const watch = (repository) => {
    showChanges(git, repository);
    repository.state.onDidChange(() => showChanges(git, repository));
  };
  git.repositories.forEach(watch);
  git.onDidOpenRepository(watch);
}

async function openPrContainer() {
  try {
    await vscode.commands.executeCommand(PR_CONTAINER);
  } catch (error) {
    console.warn('guild-review-pr-tree: could not open the PR side bar', error);
  }
}

// The PR's changes as the worktree has them now. The GitHub Pull Requests extension's own changes view keeps the file
// list it fetched first, so after bridge moved the review branch it showed an old diff; this view is rebuilt each
// time HEAD moves.
async function showChanges(git, repository) {
  const settings = vscode.workspace.getConfiguration('guild');
  const match = PR_URL.exec(settings.get('reviewPullRequest') || '');
  const base = settings.get('reviewBase') || '';
  const head = repository.state.HEAD?.commit;
  if (!match || !base || !head || head === shownHead || showing) return;
  showing = true;
  try {
    try {
      await repository.fetch({ remote: 'origin', ref: base });
    } catch (error) {
      console.warn('guild-review-pr-tree: could not fetch the base branch; diffing against the local copy', error);
    }
    const mergeBase = await repository.getMergeBase(`origin/${base}`, head);
    if (!mergeBase) return;
    const changes = await repository.diffBetween(mergeBase, head);
    const entries = changes.map((change) => [
      change.uri,
      ADDED.has(change.status) ? undefined : git.toGitUri(change.originalUri, mergeBase),
      DELETED.has(change.status) ? undefined : git.toGitUri(change.uri, head),
    ]);
    const prefix = `Changes in Pull Request #${match[3]}`;
    const title = `${prefix} at ${head.slice(0, 9)}`;
    await vscode.commands.executeCommand('vscode.changes', title, entries);
    shownHead = head;
    // An older head's view, or the GitHub Pull Requests extension's own, would show a diff that is no longer the PR's.
    const stale = vscode.window.tabGroups.all
      .flatMap((group) => group.tabs)
      .filter((tab) => tab.label.startsWith(prefix) && tab.label !== title);
    if (stale.length) await vscode.window.tabGroups.close(stale);
  } catch (error) {
    console.warn('guild-review-pr-tree: could not show the PR changes', error);
  } finally {
    showing = false;
  }
  // HEAD may have moved again while this view was built.
  if (shownHead === head && repository.state.HEAD?.commit !== head) showChanges(git, repository);
}

module.exports = { activate, deactivate() {} };
