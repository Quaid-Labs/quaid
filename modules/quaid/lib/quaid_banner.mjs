export function renderQuaidBanner(C, options = {}) {
  const subtitle = options.subtitle || "";
  const title = options.title || " INTERACTIVE CONFIG EDITOR ";
  const topRightTail = options.topRightTail || "                                      ";
  const leftShift = Number.isFinite(Number(options.leftShift)) ? Number(options.leftShift) : 0;
  const footerRight = options.footerRight || "";
  const footerLines = Array.isArray(options.footerLines) ? options.footerLines : [];
  const pad = (count) => " ".repeat(Math.max(0, Number(count || 0) - leftShift));
  const metaRightEdge = Math.max(0, 61 - leftShift);

  const lines = [
    "",
    C.dim(`${pad(5)}·          ✦                          ·${topRightTail}`),
    C.dim(`${pad(0)}✧        ·  `) + C.bmag("  ██████    ██    ██   ██████   ██  ██████") + C.dim("   ·        ✧"),
    C.dim(`${pad(7)}✦    `) + C.bmag(" ██    ██   ██    ██  ██    ██  ██  ██   ██") + C.dim("      ✦"),
    C.dim(`${pad(2)}·         `) + C.bmag(" ██    ██   ██    ██  ████████  ██  ██   ██") + C.dim("        ·"),
    C.dim(`${pad(5)}·      `) + C.bmag(" ██ ▄▄ ██   ██    ██  ██    ██  ██  ██   ██") + C.dim("      ·"),
    C.dim(`${pad(0)}✦           `) + C.bmag("  ██████    ▀██████▀  ██    ██  ██  ██████ ") + C.dim("      ✦"),
    C.dim(`${pad(7)}✧    `) + C.bmag("     ▀▀") + C.dim("                                       ·        ✧"),
  ];

  if (subtitle) {
    const subtitlePad = Math.max(0, metaRightEdge - subtitle.length);
    lines.push(" ".repeat(subtitlePad) + C.dim(subtitle));
  }

  if (footerRight) {
    const rightPad = Math.max(0, metaRightEdge - String(footerRight).length);
    lines.push(" ".repeat(rightPad) + C.dim(String(footerRight)));
  }

  for (const line of footerLines) {
    lines.push(line);
  }

  lines.push(
    "",
    pad(16) + C.dim("· ") + C.cyan("░▒▓") + C.bold(title) + C.cyan("▓▒░") + C.dim(" ·"),
    "",
  );

  return lines;
}
