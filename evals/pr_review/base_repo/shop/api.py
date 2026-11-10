"""Request handlers. Each takes a connection, the caller and parameters."""

import os
import sqlite3

from shop import db
from shop.auth import User, require_active, require_admin
from shop.pagination import clamp_per_page, offset_for


def list_products(conn: sqlite3.Connection, user: User | None, page: int, per_page: int) -> dict:
    per_page = clamp_per_page(per_page)
    rows = db.list_products(conn, limit=per_page, offset=offset_for(page, per_page))
    return {"page": page, "items": [dict(row) for row in rows]}


def get_product(conn: sqlite3.Connection, user: User | None, product_id: int) -> dict:
    row = db.get_product(conn, product_id)
    if row is None:
        raise LookupError(f"product {product_id} not found")
    return dict(row)


def set_stock(conn: sqlite3.Connection, user: User | None, product_id: int, quantity: int) -> dict:
    require_admin(user)
    if quantity < 0:
        raise ValueError("stock cannot be negative")
    with db.transaction(conn):
        conn.execute("UPDATE products SET stock = ? WHERE id = ?", (quantity, product_id))
    return {"product_id": product_id, "stock": quantity}


def export_report(user: User | None, export_dir: str, report_name: str, body: str) -> str:
    require_admin(user)
    safe_name = os.path.basename(report_name)
    if not safe_name or safe_name.startswith("."):
        raise ValueError("invalid report name")
    path = os.path.join(export_dir, safe_name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


def my_orders(conn: sqlite3.Connection, user: User | None) -> list[dict]:
    user = require_active(user)
    rows = conn.execute(
        "SELECT id, total_cents, created_at FROM orders WHERE user_id = ? ORDER BY id DESC",
        (user.id,),
    ).fetchall()
    return [dict(row) for row in rows]
