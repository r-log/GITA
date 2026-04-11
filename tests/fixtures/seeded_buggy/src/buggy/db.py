"""Database access layer for the seeded_buggy fixture.

Deliberately dense with classic SQL / credential mistakes so the onboarding
agent's checklist can prove it catches them.
"""
import sqlite3


# PLANTED ISSUE: hardcoded database password in source.
DB_PASSWORD = "admin123"


def get_user_by_name(conn, name):
    # PLANTED ISSUE: SQL injection via f-string formatting.
    query = f"SELECT * FROM users WHERE name = '{name}'"
    return conn.execute(query).fetchall()


def get_user_by_id(conn, user_id):
    # PLANTED ISSUE: same SQL injection pattern, different column.
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return conn.execute(query).fetchall()


def delete_user(conn, name):
    # PLANTED ISSUE: another string-concat injection; also no commit.
    conn.execute("DELETE FROM users WHERE name = '" + name + "'")
    # Note: no conn.commit() — silent data loss on rollback.
