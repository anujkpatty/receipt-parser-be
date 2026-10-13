import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("EXPENSES_DB_PATH", "expenses.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    store_name TEXT NOT NULL,
    date TEXT NOT NULL,
    total REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    price REAL NOT NULL,
    category TEXT NOT NULL,
    date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    category TEXT,
    period TEXT NOT NULL,
    "limit" REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    receipt_id INTEGER REFERENCES receipts(id) ON DELETE CASCADE,
    rule_id INTEGER REFERENCES rules(id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT,
    message TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_items_user_date ON items(user_id, date);
CREATE INDEX IF NOT EXISTS idx_flags_user ON flags(user_id, acknowledged);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)


# === Receipts ===

def save_receipt(user_id, store_name, date, total, items):
    """items: list of (name, price, category). Returns receipt id."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO receipts (user_id, store_name, date, total) VALUES (?, ?, ?, ?)",
            (user_id, store_name, date, total),
        )
        receipt_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO items (receipt_id, user_id, name, price, category, date) VALUES (?, ?, ?, ?, ?, ?)",
            [(receipt_id, user_id, name, price, category, date) for name, price, category in items],
        )
        return receipt_id


def spend_between(user_id, start, end, category=None):
    """Total item spend for dates in [start, end] (inclusive, YYYY-MM-DD)."""
    query = "SELECT COALESCE(SUM(price), 0) FROM items WHERE user_id = ? AND date BETWEEN ? AND ?"
    params = [user_id, start, end]
    if category:
        query += " AND category = ?"
        params.append(category)
    with connect() as conn:
        return conn.execute(query, params).fetchone()[0]


def first_spend_date(user_id):
    with connect() as conn:
        return conn.execute("SELECT MIN(date) FROM items WHERE user_id = ?", (user_id,)).fetchone()[0]


def spend_by_category(user_id, start, end):
    with connect() as conn:
        rows = conn.execute(
            "SELECT category, ROUND(SUM(price), 2) AS spend, COUNT(*) AS item_count FROM items "
            "WHERE user_id = ? AND date BETWEEN ? AND ? GROUP BY category ORDER BY spend DESC",
            (user_id, start, end),
        ).fetchall()
        return [dict(r) for r in rows]


def recent_receipts(user_id, limit):
    with connect() as conn:
        rows = conn.execute(
            "SELECT r.id, r.store_name, r.date, r.total, "
            "GROUP_CONCAT(i.category || ':' || i.price, ', ') AS items "
            "FROM receipts r LEFT JOIN items i ON i.receipt_id = r.id "
            "WHERE r.user_id = ? GROUP BY r.id ORDER BY r.date DESC, r.id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# === Rules ===

def add_rule(user_id, category, period, limit):
    with connect() as conn:
        cur = conn.execute(
            'INSERT INTO rules (user_id, category, period, "limit") VALUES (?, ?, ?, ?)',
            (user_id, category, period, limit),
        )
        return cur.lastrowid


def list_rules(user_id):
    with connect() as conn:
        rows = conn.execute(
            'SELECT id, category, period, "limit" FROM rules WHERE user_id = ? ORDER BY id',
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_rule(user_id, rule_id):
    with connect() as conn:
        cur = conn.execute("DELETE FROM rules WHERE id = ? AND user_id = ?", (rule_id, user_id))
        return cur.rowcount > 0


# === Flags ===

def add_flag(user_id, receipt_id, source, severity, category, message, rule_id=None):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO flags (user_id, receipt_id, rule_id, source, severity, category, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, receipt_id, rule_id, source, severity, category, message),
        )
        return cur.lastrowid


def list_flags(user_id, include_acknowledged=False):
    query = (
        "SELECT id, receipt_id, rule_id, source, severity, category, message, acknowledged, created_at "
        "FROM flags WHERE user_id = ?"
    )
    if not include_acknowledged:
        query += " AND acknowledged = 0"
    query += " ORDER BY id DESC"
    with connect() as conn:
        return [dict(r) for r in conn.execute(query, (user_id,)).fetchall()]


def acknowledge_flag(user_id, flag_id):
    with connect() as conn:
        cur = conn.execute(
            "UPDATE flags SET acknowledged = 1 WHERE id = ? AND user_id = ?", (flag_id, user_id)
        )
        return cur.rowcount > 0
