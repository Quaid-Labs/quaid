#!/usr/bin/env python3
"""
Enable WAL (Write-Ahead Logging) mode for better concurrency.
Run once to upgrade the database for concurrent access.
"""

import os
import sqlite3
from pathlib import Path
from lib.runtime_context import get_data_dir

def _default_db_path() -> Path:
    env_path = os.environ.get("MEMORY_DB_PATH")
    if env_path:
        return Path(env_path)
    return get_data_dir() / "memory.db"

def enable_wal_mode():
    """Enable WAL mode and optimize for concurrent access."""
    db_path = _default_db_path()
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return

    print(f"Upgrading database at {db_path} for better concurrency...")

    try:
        with sqlite3.connect(db_path) as conn:
            # Check current configuration
            current_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            print(f"Current journal mode: {current_mode}")

            # Enable WAL mode (persistent per database file)
            conn.execute("PRAGMA journal_mode=WAL")
            new_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

            # These are session-scoped for this connection and are reported as such.
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-64000")
            conn.execute("PRAGMA temp_store=MEMORY")

            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            sync = conn.execute("PRAGMA synchronous").fetchone()[0]

            print("✅ Upgraded successfully:")
            print(f"   Journal mode: {current_mode} → {new_mode}")
            print(f"   Busy timeout (session): {timeout}ms")
            print(f"   Synchronous (session): {sync}")
            print("   Cache size (session): 64MB")
    except sqlite3.Error as exc:
        print(f"Failed enabling WAL mode: {exc}")
        raise
    
    print("\n🔄 Benefits:")
    print("   - Multiple readers can read simultaneously")
    print("   - Writers don't block readers")
    print("   - Better resilience under concurrent reads/writes")

if __name__ == "__main__":
    enable_wal_mode()
