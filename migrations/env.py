"""Alembic environment: uses the app's own engine and models, so migrations always target DATABASE_URL."""
from alembic import context

from app import db

target_metadata = db.Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=db.URL, target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=db.IS_SQLITE, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # A connection may be handed over by app/migrations.py (which holds the migration lock).
    connection = context.config.attributes.get("connection")
    if connection is None:
        with db.engine.connect() as connection:
            _run(connection)
    else:
        _run(connection)


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      render_as_batch=db.IS_SQLITE,  # SQLite can't ALTER most things; batch mode rebuilds tables
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
