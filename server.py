"""Local demo wallet with a small SQLite database (Python standard library only)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("WALLET_DB_PATH", ROOT / "data" / "wallet.sqlite3"))
RATE = 9240  # 1 USDT = 92.40 RUB, in hundredths
INITIAL = {"RUB": 5_000_000, "USDT": 25_040, "EUR": 125_000, "USD": 32_000, "GBP": 8_000}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init_db(reset: bool = False) -> None:
    with connect() as db:
        if reset:
            db.executescript("DROP TABLE IF EXISTS orders; DROP TABLE IF EXISTS balances;")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS balances (
              currency TEXT PRIMARY KEY,
              amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0)
            );
            CREATE TABLE IF NOT EXISTS orders (
              order_id TEXT PRIMARY KEY,
              idempotency_key TEXT NOT NULL UNIQUE,
              request_hash TEXT NOT NULL,
              source_currency TEXT NOT NULL,
              target_currency TEXT NOT NULL,
              source_minor INTEGER NOT NULL,
              target_minor INTEGER NOT NULL,
              fee_minor INTEGER NOT NULL,
              rate INTEGER NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('COMPLETED')),
              created_at TEXT NOT NULL
            );
        """)
        db.executemany("INSERT OR IGNORE INTO balances(currency, amount_minor) VALUES (?, ?)", INITIAL.items())


def rounded_div(n: int, d: int) -> int:
    return (n * 2 + d) // (2 * d)


def public_order(row: sqlite3.Row) -> dict:
    return {
        "orderId": row["order_id"], "sourceCurrency": row["source_currency"],
        "targetCurrency": row["target_currency"], "sourceMinor": row["source_minor"],
        "targetMinor": row["target_minor"], "feeMinor": row["fee_minor"],
        "rate": row["rate"], "status": row["status"], "createdAt": row["created_at"],
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, status: int, value: dict | list):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size > 16_384:
            raise ValueError("Слишком большой запрос")
        data = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Ожидался JSON-объект")
        return data

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/v1/wallet":
            with connect() as db:
                balances = {row["currency"]: row["amount_minor"] for row in db.execute("SELECT * FROM balances")}
            return self.send_json(200, {"balances": balances, "storage": "SQLite"})
        if path == "/api/v1/exchange-orders":
            with connect() as db:
                rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 100").fetchall()
            return self.send_json(200, [public_order(row) for row in rows])
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/v1/exchange-orders":
            return self.send_json(404, {"error": "NOT_FOUND", "message": "Маршрут не найден"})
        try:
            data = self.read_json()
            source, target = data.get("sourceCurrency"), data.get("targetCurrency")
            amount = data.get("sourceMinor")
            key = self.headers.get("Idempotency-Key", "")
            if source not in ("RUB", "USDT") or target not in ("RUB", "USDT") or source == target:
                return self.send_json(400, {"error": "INVALID_PAIR", "message": "Поддерживаются только RUB ↔ USDT"})
            if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0 or amount > 10**12:
                return self.send_json(400, {"error": "INVALID_AMOUNT", "message": "Сумма должна быть положительным целым числом в минимальных единицах"})
            if not key or len(key) > 128:
                return self.send_json(400, {"error": "IDEMPOTENCY_KEY_REQUIRED", "message": "Нужен Idempotency-Key"})
            request_hash = hashlib.sha256(f"{source}:{target}:{amount}".encode()).hexdigest()
            db = connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute("SELECT * FROM orders WHERE idempotency_key=?", (key,)).fetchone()
                if previous:
                    db.rollback()
                    if previous["request_hash"] != request_hash:
                        return self.send_json(409, {"error": "IDEMPOTENCY_CONFLICT", "message": "Ключ уже использован для другой операции"})
                    return self.send_json(200, {"order": public_order(previous), "balances": self.get_balances()})
                source_balance = db.execute("SELECT amount_minor FROM balances WHERE currency=?", (source,)).fetchone()[0]
                fee = 5000 if source == "RUB" else rounded_div(5000 * 100, RATE)
                if amount > source_balance:
                    db.rollback()
                    return self.send_json(422, {"error": "INSUFFICIENT_FUNDS", "message": "Недостаточно средств"})
                if amount <= fee:
                    db.rollback()
                    return self.send_json(422, {"error": "AMOUNT_BELOW_FEE", "message": "Сумма должна быть больше комиссии"})
                net = amount - fee
                received = rounded_div(net * 100, RATE) if source == "RUB" else rounded_div(net * RATE, 100)
                if received < 1:
                    db.rollback()
                    return self.send_json(422, {"error": "AMOUNT_TOO_SMALL", "message": "Сумма получения меньше минимальной"})
                db.execute("UPDATE balances SET amount_minor=amount_minor-? WHERE currency=?", (amount, source))
                db.execute("UPDATE balances SET amount_minor=amount_minor+? WHERE currency=?", (received, target))
                order_id = "EX-" + uuid.uuid4().hex[:12].upper()
                created = now()
                db.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'COMPLETED', ?)",
                           (order_id, key, request_hash, source, target, amount, received, fee, RATE, created))
                row = db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
                db.commit()
                balances = {r["currency"]: r["amount_minor"] for r in db.execute("SELECT * FROM balances")}
                return self.send_json(201, {"order": public_order(row), "balances": balances})
            finally:
                db.close()
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            return self.send_json(400, {"error": "INVALID_REQUEST", "message": str(exc)})
        except Exception:
            return self.send_json(500, {"error": "INTERNAL_ERROR", "message": "Не удалось выполнить обмен"})

    def get_balances(self):
        with connect() as db:
            return {row["currency"]: row["amount_minor"] for row in db.execute("SELECT * FROM balances")}


if __name__ == "__main__":
    init_db(reset="--reset-db" in sys.argv)
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8000"))), Handler)
    print(f"Локальный прототип: http://127.0.0.1:{server.server_port}")
    print(f"SQLite: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен")

