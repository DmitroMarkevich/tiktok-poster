from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from .models import Base
from config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, echo=False)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session_factory() as session:
        yield session
