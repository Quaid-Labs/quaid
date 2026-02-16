#!/usr/bin/env python3
"""
Quaid Memory Plugin — Release Sanitizer

Sanitizes the plugin directory for public release by:
- Replacing personal identifiers with generic/themed alternatives
- Removing sensitive files (DB, .env, personal projects)
- Validating output for leaks

Usage:
  python3 sanitize-release.py --dry-run
  python3 sanitize-release.py --output-dir build/quaid-release/
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Set


# =============================================================================
# Configuration
# =============================================================================

# Personal identifiers to replace
REPLACEMENTS = {
    # Personal names
    r'\bAlfie\b': 'Assistant',
    r'\balfie\b': 'assistant',
    r'\bALFIE\b': 'ASSISTANT',
    r'\bSolomon\b(?! Steadman)': 'User',  # "Solomon" alone → "User"
    r'Solomon Steadman': 'Default User',
    r'\bsolomon\b': 'default',

    # Locations
    r'\bVilla Atmata\b': 'Home',
    r'\bvilla-atmata\b': 'home',

    # Paths
    r'~/<username>/clawd': '${QUAID_WORKSPACE}',
    r'~/clawd': '${QUAID_WORKSPACE}',
    r'/Volumes/Alfie': '${QUAID_WORKSPACE}',

    # Owner IDs (in config contexts)
    r'"solomon"': '"default"',
    r"'solomon'": "'default'",

    # Keychain access (remove entirely - use env vars only)
    # TypeScript/JavaScript pattern (handles both direct and compiled forms)
    r'\(0, node_child_process_1\.execSync\)\("security find-generic-password[^"]+"\s*,\s*\{[^}]+\}\)\.trim\(\)': 'process.env.ANTHROPIC_API_KEY || ""',
    r'execSync\("security find-generic-password[^"]+"\s*,\s*\{[^}]+\}\)\.trim\(\)': 'process.env.ANTHROPIC_API_KEY || ""',
    # Python pattern
    r'subprocess\.check_output\(\["security", "find-generic-password"[^\]]+\]\)\.decode\([^)]*\)\.strip\(\)': 'os.environ.get("ANTHROPIC_API_KEY", "")',
}

# Example content replacements (for README, config templates)
EXAMPLE_REPLACEMENTS = {
    # Total Recall themed examples
    'Example person': 'Quaid',
    'example_person': 'quaid',
    'User prefers': 'Quaid prefers',
    'The user': 'Quaid',
}

# File patterns to exclude from copying
EXCLUDE_PATTERNS = [
    # Data files
    '*.db',
    '*.db-*',
    '.env',
    '.env.*',

    # Development files
    'tests/',
    '__pycache__/',
    '*.pyc',
    '*.pyo',
    '.pytest_cache/',
    '.coverage',
    'htmlcov/',

    # Personal content
    'journal/',
    'memory/',
    'projects/',  # Personal projects, not the projects system code

    # Build artifacts
    'node_modules/',
    'dist/',
    'build/',
    '.DS_Store',

    # Seed/test data
    'seed*.py',
    'test_recall.py',

    # Dev artifacts
    'index-fixed.ts',
    'test-runner.js',
    'vitest.config.ts',
    'clawdbot.plugin.json',
    'package-lock.json',

    # Backups
    '*.bak',
    '*.backup',

    # Migration files
    'migrations/',

    # Stress test isolated env
    'memory-stress-test/',
]

# File extensions to process for text replacement
TEXT_EXTENSIONS = {
    '.py', '.ts', '.js', '.json', '.md', '.sh', '.sql', '.txt',
    '.yml', '.yaml', '.toml', '.cfg', '.conf', '.ini'
}

# Patterns that indicate a leak (should not appear in sanitized output)
LEAK_PATTERNS = [
    (r'\bAlfie\b(?!-)', 'Personal name: Alfie'),  # Allow "Alfie-backup" style but not "Alfie" alone
    (r'\bSolomon Steadman\b', 'Personal name: Solomon Steadman'),
    (r'~/<username>/clawd(?!\$)', 'Hardcoded path: ~/<username>/clawd'),  # Allow in comments about migration
    (r'/Volumes/Alfie', 'Hardcoded NAS path: /Volumes/Alfie'),
    (r'find-generic-password', 'Keychain access (should use env vars)'),
    (r'ANTHROPIC_API_KEY\s*=\s*["\']sk-ant-', 'API key leak'),
    (r'@SolomonS\b', 'Personal Telegram handle'),
    (r'\bsolomon-(?!quaid)', 'Personal identifier in code'),  # Allow "solomon-quaid" examples
]


# =============================================================================
# Helpers
# =============================================================================

def should_exclude(path: Path, base: Path) -> bool:
    """Check if path matches any exclude pattern."""
    rel = path.relative_to(base)
    rel_str = str(rel)

    for pattern in EXCLUDE_PATTERNS:
        if pattern.endswith('/'):
            # Directory pattern
            if rel_str.startswith(pattern) or f'/{pattern}' in f'/{rel_str}/':
                return True
        elif '*' in pattern:
            # Wildcard pattern
            import fnmatch
            if fnmatch.fnmatch(path.name, pattern):
                return True
        else:
            # Exact match
            if rel_str == pattern or rel_str.startswith(f'{pattern}/'):
                return True

    return False


def is_text_file(path: Path) -> bool:
    """Check if file should have text replacements applied."""
    return path.suffix.lower() in TEXT_EXTENSIONS


def _strip_keychain_block(content: str) -> str:
    """Remove the entire keychain fallback block from llm_clients.py."""
    # Match the keychain fallback block (Python subprocess.run pattern)
    pattern = (
        r'\n    # Keychain fallback \(Anthropic only, macOS only\)\n'
        r'    if env_var_name == "ANTHROPIC_API_KEY":.*?'
        r"raise RuntimeError\(\"'security' command not found \(not on macOS\?\)\"\)\n"
    )
    content = re.sub(pattern, '\n', content, flags=re.DOTALL)
    return content


def sanitize_text(content: str, filepath: Path) -> str:
    """Apply all text replacements to content."""
    original = content

    # Apply main replacements
    for pattern, replacement in REPLACEMENTS.items():
        content = re.sub(pattern, replacement, content)

    # Remove keychain fallback block entirely
    if filepath.name == 'llm_clients.py':
        content = _strip_keychain_block(content)

    # Apply example replacements only in README and example files
    if 'README' in filepath.name or 'example' in filepath.name.lower():
        for pattern, replacement in EXAMPLE_REPLACEMENTS.items():
            content = re.sub(re.escape(pattern), replacement, content)

    return content


def validate_syntax(filepath: Path, content: str) -> Tuple[bool, str]:
    """
    Validate that file is still syntactically valid after sanitization.
    Returns (is_valid, error_message).
    """
    if filepath.suffix == '.py':
        try:
            compile(content, str(filepath), 'exec')
            return True, ""
        except SyntaxError as e:
            return False, f"Python syntax error: {e}"

    elif filepath.suffix == '.json':
        try:
            json.loads(content)
            return True, ""
        except json.JSONDecodeError as e:
            return False, f"JSON parse error: {e}"

    # For other files, assume valid
    return True, ""


def detect_leaks(content: str, filepath: Path) -> List[str]:
    """
    Check for personal data leaks in content.
    Returns list of leak descriptions found.
    """
    leaks = []

    for pattern, description in LEAK_PATTERNS:
        if re.search(pattern, content):
            leaks.append(f"{description} in {filepath}")

    return leaks


def copy_tree(src: Path, dst: Path, dry_run: bool = False) -> Tuple[int, int, List[str]]:
    """
    Copy source tree to destination with sanitization.
    Returns (files_copied, files_skipped, errors).
    """
    files_copied = 0
    files_skipped = 0
    errors = []

    for item in src.rglob('*'):
        if item.is_file():
            # Check exclusion
            if should_exclude(item, src):
                files_skipped += 1
                continue

            # Determine destination path
            rel_path = item.relative_to(src)
            dest_path = dst / rel_path

            if dry_run:
                print(f"  Would copy: {rel_path}")
                files_copied += 1
                continue

            # Create parent directory
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            # Read and process
            try:
                if is_text_file(item):
                    content = item.read_text(encoding='utf-8')
                    sanitized = sanitize_text(content, item)

                    # Validate syntax
                    is_valid, error_msg = validate_syntax(item, sanitized)
                    if not is_valid:
                        errors.append(f"{rel_path}: {error_msg}")
                        continue

                    dest_path.write_text(sanitized, encoding='utf-8')
                else:
                    # Binary copy
                    shutil.copy2(item, dest_path)

                files_copied += 1

            except Exception as e:
                errors.append(f"{rel_path}: {e}")
                files_skipped += 1

    return files_copied, files_skipped, errors


def validate_output(output_dir: Path) -> Tuple[bool, List[str]]:
    """
    Scan output directory for personal data leaks.
    Returns (is_clean, leak_list).
    """
    all_leaks = []

    for item in output_dir.rglob('*'):
        if item.is_file() and is_text_file(item):
            try:
                content = item.read_text(encoding='utf-8')
                leaks = detect_leaks(content, item.relative_to(output_dir))
                all_leaks.extend(leaks)
            except Exception:
                # Skip files that can't be read as text
                pass

    return len(all_leaks) == 0, all_leaks


def print_summary(files_copied: int, files_skipped: int, errors: List[str],
                  output_dir: Path, is_clean: bool, leaks: List[str]) -> None:
    """Print sanitization summary."""
    print("\n" + "=" * 70)
    print("SANITIZATION SUMMARY")
    print("=" * 70)

    print(f"\nFiles processed:")
    print(f"  Copied:  {files_copied}")
    print(f"  Skipped: {files_skipped}")

    if errors:
        print(f"\nErrors ({len(errors)}):")
        for error in errors[:10]:  # Show first 10
            print(f"  ✗ {error}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")

    print(f"\nOutput directory: {output_dir}")

    if output_dir.exists():
        total_size = sum(f.stat().st_size for f in output_dir.rglob('*') if f.is_file())
        print(f"Total size: {total_size / (1024 * 1024):.1f} MB")

    print("\nValidation:")
    if is_clean:
        print("  ✓ No personal data leaks detected")
    else:
        print(f"  ✗ {len(leaks)} potential leak(s) found:")
        for leak in leaks[:10]:
            print(f"    - {leak}")
        if len(leaks) > 10:
            print(f"    ... and {len(leaks) - 10} more")

    print("\n" + "=" * 70)


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Sanitize Quaid plugin for public release',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview what would be sanitized (no files written)
  python3 sanitize-release.py --dry-run

  # Sanitize to default build directory
  python3 sanitize-release.py

  # Sanitize to custom location
  python3 sanitize-release.py --output-dir /tmp/quaid-clean

  # Sanitize from non-standard source
  python3 sanitize-release.py --source-dir ../my-quaid-fork
        """
    )

    parser.add_argument(
        '--source-dir',
        type=Path,
        help='Source directory (default: auto-detect from script location)'
    )

    parser.add_argument(
        '--output-dir',
        type=Path,
        default=Path('build/quaid-release'),
        help='Output directory (default: build/quaid-release/)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview changes without writing files'
    )

    parser.add_argument(
        '--force',
        action='store_true',
        help='Overwrite output directory without prompting'
    )

    args = parser.parse_args()

    # Determine source directory
    if args.source_dir:
        source_dir = args.source_dir.resolve()
    else:
        # Auto-detect: script is in scripts/release-templates/, source is ../../plugins/quaid/
        script_dir = Path(__file__).parent.resolve()
        source_dir = script_dir.parent.parent / 'plugins' / 'quaid'

    if not source_dir.exists():
        print(f"Error: Source directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir = args.output_dir.resolve()

    # Print header
    print("=" * 70)
    print("QUAID RELEASE SANITIZER")
    print("=" * 70)
    print(f"\nSource:  {source_dir}")
    print(f"Output:  {output_dir}")
    print(f"Mode:    {'DRY RUN (no files written)' if args.dry_run else 'LIVE'}")
    print()

    # Confirm if not dry-run and output exists
    if not args.dry_run and output_dir.exists():
        if args.force:
            shutil.rmtree(output_dir)
        else:
            print(f"Warning: Output directory already exists: {output_dir}")
            response = input("Overwrite? [y/N] ").strip().lower()
            if response != 'y':
                print("Aborted.")
                sys.exit(0)
            shutil.rmtree(output_dir)

    # Copy and sanitize
    print("Processing files...")
    files_copied, files_skipped, errors = copy_tree(source_dir, output_dir, args.dry_run)

    # Validate output (unless dry-run)
    if args.dry_run:
        is_clean, leaks = True, []
        print("\n(Validation skipped in dry-run mode)")
    else:
        print("\nValidating output for leaks...")
        is_clean, leaks = validate_output(output_dir)

    # Print summary
    print_summary(files_copied, files_skipped, errors, output_dir, is_clean, leaks)

    # Exit code
    if not is_clean or errors:
        print("\n⚠️  SANITIZATION FAILED — DO NOT RELEASE", file=sys.stderr)
        sys.exit(1)
    elif args.dry_run:
        print("\n✓ Dry run complete")
        sys.exit(0)
    else:
        print("\n✓ Sanitization complete — safe to package")
        sys.exit(0)


if __name__ == '__main__':
    main()
