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

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS inbound_ports (
  inbound_id INTEGER PRIMARY KEY,
  port INTEGER NOT NULL
);
'''

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        await db.executescript(SCHEMA)
        await db.commit()
        # Migrate: add is_test column if missing
        try:
            await db.execute("ALTER TABLE configs ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0")
            await db.commit()
        except Exception:
            pass

async def register_user(numeric_id: int, telegram_user_id: int, default_limit: int) -> None:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        await db.execute('INSERT OR IGNORE INTO users (numeric_id, telegram_user_id, max_configs) VALUES (?, ?, ?)', (numeric_id, telegram_user_id, default_limit))
        await db.commit()

async def set_user_limit(numeric_id: int, max_configs: int) -> None:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        # Ensure user row exists; if not, insert with this limit
        await db.execute('INSERT OR IGNORE INTO users (numeric_id, telegram_user_id, max_configs) VALUES (?, COALESCE((SELECT telegram_user_id FROM users WHERE numeric_id = ?), 0), ?)', (numeric_id, numeric_id, max_configs))
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

async def add_config_record(numeric_id: int, telegram_user_id: int, inbound_id: int, client_identifier: str, client_id: str, total_bytes: int, expiry_days: int, raw_response: str, is_test: int = 0) -> None:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        try:
            await db.execute('INSERT INTO configs (numeric_id, telegram_user_id, inbound_id, client_identifier, client_id, total_bytes, expiry_days, raw_response, is_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (numeric_id, telegram_user_id, inbound_id, client_identifier, client_id, total_bytes, expiry_days, raw_response, is_test))
        except Exception:
            await db.execute('INSERT INTO configs (numeric_id, telegram_user_id, inbound_id, client_identifier, client_id, total_bytes, expiry_days, raw_response) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (numeric_id, telegram_user_id, inbound_id, client_identifier, client_id, total_bytes, expiry_days, raw_response))
        await db.commit()

async def get_configs_by_numeric_id(numeric_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM configs WHERE numeric_id = ? ORDER BY created_at DESC', (numeric_id,)) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_configs_by_numeric_id_since(numeric_id: int, since_iso: str) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute('SELECT * FROM configs WHERE numeric_id = ? AND created_at >= ? ORDER BY created_at DESC', (numeric_id, since_iso)) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
        except Exception:
            # if created_at is not ISO comparable, fallback to all
            async with db.execute('SELECT * FROM configs WHERE numeric_id = ? ORDER BY created_at DESC', (numeric_id,)) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

async def get_latest_config_by_identifier(client_identifier: str) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM configs WHERE client_identifier = ? ORDER BY created_at DESC LIMIT 1', (client_identifier,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def count_test_configs_by_telegram_user(telegram_user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        async with db.execute('SELECT COUNT(*) FROM configs WHERE telegram_user_id = ? AND COALESCE(is_test,0)=1', (telegram_user_id,)) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

async def get_user_config_stats() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        db.row_factory = aiosqlite.Row
        query = (
            'SELECT u.numeric_id AS numeric_id, '
            'u.telegram_user_id AS telegram_user_id, '
            'COUNT(c.id) AS configs_count, '
            'COALESCE(SUM(c.total_bytes), 0) AS total_bytes_sum, '
            'MIN(c.created_at) AS first_created_at, '
            'MAX(c.created_at) AS last_created_at '
            'FROM users u '
            'JOIN configs c ON c.numeric_id = u.numeric_id '
            'WHERE COALESCE(c.is_test,0)=0 '
            'GROUP BY u.numeric_id, u.telegram_user_id '
            'ORDER BY last_created_at DESC'
        )
        async with db.execute(query) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_all_configs_non_test() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT numeric_id, client_identifier, total_bytes '
            'FROM configs WHERE COALESCE(is_test,0)=0 '
            'ORDER BY numeric_id ASC, created_at ASC'
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# Admin settings helpers
async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        await db.execute('REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
        await db.commit()

async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT value FROM settings WHERE key = ?', (key,)) as cur:
            row = await cur.fetchone()
            return row['value'] if row else None

async def get_all_settings() -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT key, value FROM settings') as cur:
            rows = await cur.fetchall()
            return {r['key']: r['value'] for r in rows}

async def set_inbound_port(inbound_id: int, port: int) -> None:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        await db.execute('REPLACE INTO inbound_ports (inbound_id, port) VALUES (?, ?)', (inbound_id, port))
        await db.commit()

async def get_inbound_port(inbound_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT port FROM inbound_ports WHERE inbound_id = ?', (inbound_id,)) as cur:
            row = await cur.fetchone()
            return int(row['port']) if row else None

async def unset_inbound_port(inbound_id: int) -> None:
    async with aiosqlite.connect(DB_PATH.as_posix()) as db:
        await db.execute('DELETE FROM inbound_ports WHERE inbound_id = ?', (inbound_id,))
        await db.commit()

