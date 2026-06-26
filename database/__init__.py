from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from .models import Base
from config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, echo=False)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


# Columns added after the initial schema — create_all() won't add these to an
# existing table, so patch them in with ALTER TABLE on startup (SQLite-friendly,
# preserves existing accounts/cookies). Add new entries here as the model grows.
_COLUMN_MIGRATIONS = {
    "accounts": {
        "last_warmup_at": "DATETIME",
        "warmup_count": "INTEGER DEFAULT 0",
    },
}


async def _migrate_columns(conn):
    for table, columns in _COLUMN_MIGRATIONS.items():
        rows = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        existing = {r[1] for r in rows.fetchall()}
        for name, ddl in columns.items():
            if name not in existing:
                await conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"
                )


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_columns(conn)


async def get_session() -> AsyncSession:
    async with async_session_factory() as session:
        yield session
