// Minimal status-bar fuel gauge. Runs `python -m tokengate.cli gauge --json` in the workspace.
const vscode = require("vscode");
const { execFile } = require("child_process");

function activate(context) {
  const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  item.command = "tokengate.gauge.refresh";
  item.text = "$(dashboard) …";
  item.show();
  let warned = "";

  const refresh = () => {
    const cfg = vscode.workspace.getConfiguration("tokengate");
    const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    execFile(cfg.get("python"), ["-m", "tokengate.cli", "gauge", "--json"], { cwd, timeout: 30000 }, (err, stdout) => {
      if (err) { item.text = "$(dashboard) tokengate ?"; item.tooltip = String(err); return; }
      let r; try { r = JSON.parse(stdout); } catch { return; }
      const left = r.percent_used == null ? null : Math.max(0, 100 - r.percent_used);
      const cells = left == null ? "?????" : "▮".repeat(Math.round(left / 20)) + "▯".repeat(5 - Math.round(left / 20));
      item.text = `$(dashboard) ${cells} ${left == null ? "?" : left.toFixed(0) + "%"}`;
      item.tooltip = `${r.message}\nFree providers: ${r.free.requests_24h} req / 24h\nSaved (30d, est.): ~${r.savings.total_cursor_tokens_avoided} tokens\nClick to refresh. Dashboard: tokengate dashboard`;
      item.backgroundColor = r.level === "reserve" || r.level === "empty"
        ? new vscode.ThemeColor("statusBarItem.errorBackground")
        : r.level === "warn" ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
      if ((r.level === "reserve" || r.level === "empty") && warned !== r.level) {
        warned = r.level;
        vscode.window.showWarningMessage(`tokengate: ${r.message}`);
      }
    });
  };

  context.subscriptions.push(item, vscode.commands.registerCommand("tokengate.gauge.refresh", refresh));
  refresh();
  const timer = setInterval(refresh, Math.max(1, vscode.workspace.getConfiguration("tokengate").get("refreshMinutes")) * 60000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
}

module.exports = { activate, deactivate() {} };
