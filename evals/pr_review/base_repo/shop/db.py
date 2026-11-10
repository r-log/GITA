"""SQLite access. Every query is parameterized."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_product(conn: sqlite3.Connection, product_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, name, price_cents, stock FROM products WHERE id = ?",
        (product_id,),
    ).fetchone()


def list_products(conn: sqlite3.Connection, limit: int, offset: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, name, price_cents, stock FROM products ORDER BY id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()


def get_user_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, email, password_hash, is_admin, is_active FROM users WHERE email = ?",
        (email.strip().lower(),),
    ).fetchone()


def decrement_stock(conn: sqlite3.Connection, product_id: int, quantity: int) -> bool:
    """Atomically take ``quantity`` units; False if there isn't enough stock."""
    cursor = conn.execute(
        "UPDATE products SET stock = stock - ? WHERE id = ? AND stock >= ?",
        (quantity, product_id, quantity),
    )
    return cursor.rowcount == 1
