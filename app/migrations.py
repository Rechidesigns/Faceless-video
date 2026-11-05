"""Run database migrations safely. Used three ways:

  * Railway pre-deploy step:  python -m app.migrations   (railway.json -> deploy.preDeployCommand)
  * App startup:               db.init() calls run()      (no-op when already up to date)
  * Manually:                  python -m app.migrations

A database lock makes concurrent runs (two replicas, pre-deploy + startup) wait for each other instead of colliding.
Databases created before migrations existed are detected and "stamped" at the baseline instead of being recreated.
"""
import sys
import threading
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from . import db

ROOT = Path(__file__).resolve().parent.parent
BASELINE = "0001_initial"          # first migration; matches the schema the app created before Alembic
LOCK_ID = 727274001                # any constant; Postgres advisory-lock key for migrations
_local_lock = threading.Lock()
_done = False


def _config(connection=None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def _ensure_pre_alembic_columns(conn) -> None:
    """Old databases may predate columns that are part of the baseline; add them before stamping."""
    cols = {c["name"] for c in inspect(conn).get_columns("users")}
    if "is_approved" not in cols:
        conn.execute(text("ALTER TABLE users ADD COLUMN is_approved BOOLEAN NOT NULL DEFAULT TRUE"))


def status() -> dict:
    with db.engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    head = ScriptDirectory.from_config(_config()).get_current_head()
    return {"current": current, "head": head, "up_to_date": current == head}


def run(verbose: bool = False) -> None:
    global _done
    with _local_lock:
        if _done:
            return
        t0 = time.time()
        with db.engine.connect() as conn:
            if not db.IS_SQLITE:
                conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_ID})
                conn.commit()
            try:
                tables = set(inspect(conn).get_table_names())
                if "alembic_version" not in tables and "users" in tables:
                    # created by an older version of the app (create_all): adopt it at the baseline
                    _ensure_pre_alembic_columns(conn)
                    conn.commit()
                    command.stamp(_config(conn), BASELINE)
                    conn.commit()
                    if verbose:
                        print(f"Existing database adopted at baseline {BASELINE}")
                before = MigrationContext.configure(conn).get_current_revision()
                command.upgrade(_config(conn), "head")
                conn.commit()
                after = MigrationContext.configure(conn).get_current_revision()
            finally:
                if not db.IS_SQLITE:
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_ID})
                    conn.commit()
        _done = True
        if verbose or before != after:
            print(f"Database migrations: {before or 'empty'} -> {after} ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    try:
        run(verbose=True)
        print("Migrations OK:", status())
    except Exception as e:  # non-zero exit makes Railway stop the deploy, keeping the old version live
        print(f"MIGRATION FAILED: {e}", file=sys.stderr)
        raise SystemExit(1)
