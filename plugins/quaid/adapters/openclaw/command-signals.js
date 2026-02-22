"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.normalizeCommandName = normalizeCommandName;
exports.extractCommandName = extractCommandName;
exports.signalLabelForCommand = signalLabelForCommand;
function normalizeCommandName(raw) {
    const s = String(raw || "").trim().toLowerCase();
    if (!s)
        return "";
    return s.startsWith("/") ? s.slice(1) : s;
}
function extractCommandName(event) {
    const direct = normalizeCommandName((event === null || event === void 0 ? void 0 : event.command) || (event === null || event === void 0 ? void 0 : event.name));
    if (direct)
        return direct;
    const text = String((event === null || event === void 0 ? void 0 : event.text) || (event === null || event === void 0 ? void 0 : event.input) || (event === null || event === void 0 ? void 0 : event.raw) || "").trim().toLowerCase();
    const m = text.match(/^\/([a-z0-9_-]+)/i);
    return m ? m[1].toLowerCase() : "";
}
function signalLabelForCommand(commandRaw) {
    const command = normalizeCommandName(commandRaw);
    if (!command)
        return null;
    if (command === "compact" || command === "compaction")
        return "CompactionSignal";
    if (command === "new")
        return "NewSignal";
    if (command === "reset" || command === "restart")
        return "ResetSignal";
    return null;
}
