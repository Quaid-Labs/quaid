export function renderQuaidBanner(C, options = {}) {
  const subtitle = options.subtitle || "";
  const title = options.title || " INTERACTIVE CONFIG EDITOR ";
  const topRightTail = options.topRightTail || "                                      ";
  const footerRight = options.footerRight || "";
  const footerLines = Array.isArray(options.footerLines) ? options.footerLines : [];
  const subtitlePad = 41;
  const subtitleRightEdge = subtitle ? subtitlePad + subtitle.length : 0;

  const lines = [
    "",
    C.dim(`        ·          ✦                          ·${topRightTail}`),
    C.dim("   ✧        ·  ") + C.bmag("  ██████    ██    ██   ██████   ██  ██████") + C.dim("   ·        ✧"),
    C.dim("          ✦    ") + C.bmag(" ██    ██   ██    ██  ██    ██  ██  ██   ██") + C.dim("      ✦"),
    C.dim("     ·         ") + C.bmag(" ██    ██   ██    ██  ████████  ██  ██   ██") + C.dim("        ·"),
    C.dim("        ·      ") + C.bmag(" ██ ▄▄ ██   ██    ██  ██    ██  ██  ██   ██") + C.dim("      ·"),
    C.dim("   ✦           ") + C.bmag("  ██████    ▀██████▀  ██    ██  ██  ██████ ") + C.dim("      ✦"),
    C.dim("          ✧    ") + C.bmag("     ▀▀") + C.dim("                                       ·        ✧"),
  ];

  if (subtitle) {
    lines.push(" ".repeat(subtitlePad) + C.dim(subtitle));
  }

  if (footerRight) {
    const rightPad = Math.max(0, subtitleRightEdge - String(footerRight).length);
    lines.push(" ".repeat(rightPad) + C.dim(String(footerRight)));
  }

  for (const line of footerLines) {
    lines.push(line);
  }

  lines.push(
    "",
    " ".repeat(16) + C.dim("· ") + C.cyan("░▒▓") + C.bold(title) + C.cyan("▓▒░") + C.dim(" ·"),
    "",
  );

  return lines;
}
