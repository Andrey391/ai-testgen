"""Alembic environment of the studio's database (testgen/db.py). Run through `python -m testgen.db upgrade`
or automatically at start; a new revision: alembic -c <config with script_location=testgen/migrations> revision."""
from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from testgen.db import metadata

config = context.config


def run() -> None:
    conn = config.attributes.get("connection")
    if conn is not None:
        context.configure(connection=conn, target_metadata=metadata, render_as_batch=conn.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.",
                                poolclass=pool.NullPool)
    with engine.connect() as c:
        context.configure(connection=c, target_metadata=metadata, render_as_batch=c.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run()
