export function renderQuaidBanner(C, options = {}) {
  const subtitle = options.subtitle || "";
  const title = options.title || " INTERACTIVE CONFIG EDITOR ";
  const topRightTail = options.topRightTail || "                                      ";
  const footerLines = Array.isArray(options.footerLines) ? options.footerLines : [];

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
    lines.push(" ".repeat(41) + C.dim(subtitle));
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

