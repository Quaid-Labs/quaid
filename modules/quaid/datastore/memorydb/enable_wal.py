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

    conn = sqlite3.connect(db_path)
    
    # Check current configuration
    current_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"Current journal mode: {current_mode}")
    
    # Enable WAL mode
    conn.execute("PRAGMA journal_mode=WAL")
    new_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    
    # Increase busy timeout to 30 seconds (handles janitor delays)
    conn.execute("PRAGMA busy_timeout=30000")
    
    # Optimize for concurrent reads
    conn.execute("PRAGMA synchronous=NORMAL")  # Faster than FULL, still safe with WAL
    conn.execute("PRAGMA cache_size=-64000")   # 64MB cache
    conn.execute("PRAGMA temp_store=MEMORY")   # Temp tables in memory
    
    # Check final configuration
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    
    print(f"✅ Upgraded successfully:")
    print(f"   Journal mode: {current_mode} → {new_mode}")
    print(f"   Busy timeout: 5000ms → {timeout}ms")
    print(f"   Synchronous: FULL → NORMAL")
    print(f"   Cache size: 64MB")
    
    conn.close()
    
    print("\n🔄 Benefits:")
    print("   - Multiple readers can read simultaneously")
    print("   - Writers don't block readers")
    print("   - 30-second timeout for write conflicts")
    print("   - Better performance overall")

if __name__ == "__main__":
    enable_wal_mode()
