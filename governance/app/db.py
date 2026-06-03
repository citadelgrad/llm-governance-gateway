from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        from .settings import settings
        _engine = create_async_engine(
            settings.database_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )

        @event.listens_for(_engine.sync_engine, "checkout")
        def reset_session_vars(dbapi_connection, connection_record, connection_proxy):
            cursor = dbapi_connection.cursor()
            cursor.execute(
                "SELECT set_config('app.current_user_id', '', false),"
                " set_config('app.current_tenant_id', '', false),"
                " set_config('app.current_scope', '', false)"
            )
            cursor.close()

    return _engine


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session
