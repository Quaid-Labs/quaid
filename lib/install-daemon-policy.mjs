export function shouldStartExtractionDaemonAfterInstall(platform) {
  return platform === "claude-code" || platform === "codex" || platform === "openclaw";
}
