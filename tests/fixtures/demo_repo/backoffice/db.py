"""Minimal sqlite-backed order storage."""

import json
import sqlite3


class Store:
    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(
            "CREATE TABLE orders ("
            "order_id TEXT PRIMARY KEY, "
            "user_id TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "total_cents INTEGER NOT NULL, "
            "lines TEXT NOT NULL)"
        )
        self._conn.commit()

    def save(self, order) -> None:
        self._conn.execute(
            "INSERT INTO orders (order_id, user_id, status, total_cents, lines) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                order.order_id,
                order.user_id,
                order.status,
                order.total_cents,
                json.dumps([list(line) for line in order.lines]),
            ),
        )
        self._conn.commit()

    def get(self, order_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT order_id, user_id, status, total_cents, lines "
            "FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "order_id": row[0],
            "user_id": row[1],
            "status": row[2],
            "total_cents": row[3],
            "lines": [tuple(line) for line in json.loads(row[4])],
        }

    def update_status(self, order_id: str, status: str) -> None:
        cursor = self._conn.execute(
            "UPDATE orders SET status = ? WHERE order_id = ?",
            (status, order_id),
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            raise KeyError(f"unknown order: {order_id}")
