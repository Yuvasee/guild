const vscode = require('vscode');

const PR_EXTENSION = 'GitHub.vscode-pull-request-github';
const PR_CONTAINER = 'workbench.view.extension.github-pull-request';

async function activate() {
  const workspaceFile = vscode.workspace.workspaceFile;
  if (!workspaceFile || !/\/review-[^/]*\.code-workspace$/.test(workspaceFile.path)) return;
  await vscode.extensions.getExtension(PR_EXTENSION)?.activate();
  await openPrContainer();
  // The "Changes In Pull Request" view appears only after the PR is fetched; a second open lands on it.
  setTimeout(openPrContainer, 5000);
}

async function openPrContainer() {
  try {
    await vscode.commands.executeCommand(PR_CONTAINER);
  } catch (error) {
    console.warn('guild-review-pr-tree: could not open the PR side bar', error);
  }
}

module.exports = { activate, deactivate() {} };
