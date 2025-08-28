import aiosqlite
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

DB_PATH = Path(__file__).resolve().parent / 'app.db'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
  numeric_id INTEGER PRIMARY KEY,
  telegram_user_id INTEGER,
  max_configs INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS configs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  numeric_id INTEGER NOT NULL,
  telegram_user_id INTEGER NOT NULL,
  inbound_id INTEGER NOT NULL,
  client_identifier TEXT,
  client_id TEXT,
  total_bytes INTEGER,
  expiry_days INTEGER,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  raw_response TEXT,
  FOREIGN KEY(numeric_id) REFERENCES users(numeric_id) ON DELETE CASCADE
);
'''

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        await db.executescript(SCHEMA)
        await db.commit()

async def register_user(numeric_id: int, telegram_user_id: int, default_limit: int) -> None:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        await db.execute('INSERT OR IGNORE INTO users (numeric_id, telegram_user_id, max_configs) VALUES (?, ?, ?)', (numeric_id, telegram_user_id, default_limit))
        await db.commit()

async def set_user_limit(numeric_id: int, max_configs: int) -> None:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        await db.execute('UPDATE users SET max_configs = ? WHERE numeric_id = ?', (max_configs, numeric_id))
        await db.commit()

async def get_user(numeric_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM users WHERE numeric_id = ?', (numeric_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def count_user_configs(numeric_id: int) -> int:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        async with db.execute('SELECT COUNT(*) FROM configs WHERE numeric_id = ?', (numeric_id,)) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

async def add_config_record(numeric_id: int, telegram_user_id: int, inbound_id: int, client_identifier: str, client_id: str, total_bytes: int, expiry_days: int, raw_response: str) -> None:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        await db.execute('INSERT INTO configs (numeric_id, telegram_user_id, inbound_id, client_identifier, client_id, total_bytes, expiry_days, raw_response) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (numeric_id, telegram_user_id, inbound_id, client_identifier, client_id, total_bytes, expiry_days, raw_response))
        await db.commit()

async def get_configs_by_numeric_id(numeric_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM configs WHERE numeric_id = ? ORDER BY created_at DESC', (numeric_id,)) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

