"""
Structured JSONL Logger for Memory System (Python)

Provides structured logging with:
- JSONL format (one JSON object per line)
- Log rotation with configurable retention
- Queryable with jq
- Console output for errors/warnings
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Optional

LOG_DIR = Path.home() / "clawd" / "logs"
ARCHIVE_DIR = LOG_DIR / "archive"
MAX_LOG_DAYS = 7

# Ensure directories exist
LOG_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

LogLevel = Literal["debug", "info", "warn", "error"]


def log(
    component: str,
    event: str,
    level: LogLevel = "info",
    **data: Any
) -> None:
    """Write a structured log entry."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "level": level,
        "component": component,
        "event": event,
        **data
    }
    
    line = json.dumps(entry, default=str) + "\n"
    log_file = LOG_DIR / f"{component}.log"
    
    try:
        with open(log_file, "a") as f:
            f.write(line)
    except Exception as e:
        print(f"[logger] Failed to write to {log_file}: {e}", file=sys.stderr)
    
    # Also print errors/warnings to console
    if level == "error":
        print(f"[{component}] ERROR: {event}", data, file=sys.stderr)
    elif level == "warn":
        print(f"[{component}] WARN: {event}", data, file=sys.stderr)


class Logger:
    """Convenience logger class with level methods."""
    
    def __init__(self, component: str):
        self.component = component
    
    def debug(self, event: str, **data: Any) -> None:
        log(self.component, event, "debug", **data)
    
    def info(self, event: str, **data: Any) -> None:
        log(self.component, event, "info", **data)
    
    def warn(self, event: str, **data: Any) -> None:
        log(self.component, event, "warn", **data)
    
    def error(self, event: str, **data: Any) -> None:
        log(self.component, event, "error", **data)


def rotate_logs() -> None:
    """Rotate logs - moves current logs to archive with date suffix."""
    today = datetime.now().strftime("%Y-%m-%d")
    
    try:
        for log_file in LOG_DIR.glob("*.log"):
            base_name = log_file.stem
            archive_path = ARCHIVE_DIR / f"{base_name}.{today}.log"
            
            try:
                # Check if file has content
                if log_file.stat().st_size == 0:
                    continue
                
                if archive_path.exists():
                    # Append to existing archive
                    with open(log_file, "r") as src:
                        content = src.read()
                    with open(archive_path, "a") as dst:
                        dst.write(content)
                    log_file.write_text("")  # Clear current log
                else:
                    log_file.rename(archive_path)
                
                print(f"[logger] Rotated {log_file.name} to archive")
            except Exception as e:
                print(f"[logger] Failed to rotate {log_file.name}: {e}", file=sys.stderr)
        
        # Clean old archives
        clean_old_archives()
    except Exception as e:
        print(f"[logger] Log rotation failed: {e}", file=sys.stderr)


def clean_old_archives() -> None:
    """Delete archives older than MAX_LOG_DAYS."""
    cutoff = datetime.now() - timedelta(days=MAX_LOG_DAYS)
    
    try:
        for archive_file in ARCHIVE_DIR.glob("*.log"):
            # Extract date from filename (e.g., memory.2026-02-01.log)
            parts = archive_file.stem.split(".")
            if len(parts) < 2:
                continue
            
            date_str = parts[-1]
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    archive_file.unlink()
                    print(f"[logger] Deleted old archive {archive_file.name}")
            except ValueError:
                continue
    except Exception as e:
        print(f"[logger] Failed to clean old archives: {e}", file=sys.stderr)


def get_log_path(component: str) -> Path:
    """Get log file path for a component."""
    return LOG_DIR / f"{component}.log"


def get_archive_dir() -> Path:
    """Get archive directory path."""
    return ARCHIVE_DIR


# Module-level loggers for common components
memory_logger = Logger("memory")
janitor_logger = Logger("janitor")
browser_logger = Logger("browser")


if __name__ == "__main__":
    # Test the logger
    import argparse
    
    parser = argparse.ArgumentParser(description="Logger utility")
    parser.add_argument("--rotate", action="store_true", help="Rotate logs")
    parser.add_argument("--test", action="store_true", help="Write test entries")
    args = parser.parse_args()
    
    if args.rotate:
        rotate_logs()
    elif args.test:
        memory_logger.info("test_event", message="This is a test log entry")
        memory_logger.warn("test_warning", message="This is a test warning")
        memory_logger.error("test_error", message="This is a test error")
        print(f"Test entries written to {get_log_path('memory')}")
    else:
        parser.print_help()
