const vscode = require('vscode');

const PR_EXTENSION = 'GitHub.vscode-pull-request-github';
const PR_CONTAINER = 'workbench.view.extension.github-pull-request';
const PR_URL = /^https:\/\/github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)$/;
const RETRY_MS = 2000;
const GIVE_UP_MS = 120000;

async function activate() {
  const workspaceFile = vscode.workspace.workspaceFile;
  if (!workspaceFile || !/\/review-[^/]*\.code-workspace$/.test(workspaceFile.path)) return;
  await vscode.extensions.getExtension(PR_EXTENSION)?.activate();
  await openPrContainer();
  // The "Changes In Pull Request" view appears only after the PR is fetched; a second open lands on it.
  setTimeout(openPrContainer, 5000);
  openPrChanges(Date.now() + GIVE_UP_MS);
}

async function openPrContainer() {
  try {
    await vscode.commands.executeCommand(PR_CONTAINER);
  } catch (error) {
    console.warn('guild-review-pr-tree: could not open the PR side bar', error);
  }
}

async function openPrChanges(deadline) {
  const match = PR_URL.exec(vscode.workspace.getConfiguration('guild').get('reviewPullRequest') || '');
  if (!match) return;
  try {
    // A bare number resolves through this window's own repository, which throws until the PR extension has read its
    // GitHub remote; owner and repo would resolve remotely first and then open nothing, without an error.
    await vscode.commands.executeCommand('pr.openChanges', { path: match[3] });
  } catch (error) {
    if (Date.now() < deadline) setTimeout(() => openPrChanges(deadline), RETRY_MS);
    else console.warn('guild-review-pr-tree: could not open the PR changes', error);
  }
}

module.exports = { activate, deactivate() {} };
