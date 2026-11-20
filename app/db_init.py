"""Create database tables. Runs on container start and for local setup."""
from __future__ import annotations

import time

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.database import engine, init_db


def wait_for_database(max_attempts: int = 30, delay: float = 1.0) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # pragma: no cover - startup path
            if attempt == max_attempts:
                raise
            print(f"waiting for database... ({attempt}/{max_attempts}) {exc}")
            time.sleep(delay)


def init_db_with_retry(max_attempts: int = 10, delay: float = 1.0) -> None:
    """Create the schema, tolerating a concurrent instance doing the same.

    Two API containers sharing one SQLite file may run create_all at the same
    moment; the loser's ``CREATE TABLE`` can race the winner's. The schema is
    identical, so retrying after the winner finishes is safe.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            init_db()
            return
        except OperationalError as exc:
            if "already exists" in str(exc).lower() and attempt < max_attempts:
                time.sleep(delay)
                continue
            raise


if __name__ == "__main__":
    wait_for_database()
    init_db_with_retry()
    print("database schema ready")
