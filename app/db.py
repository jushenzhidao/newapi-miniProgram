"""数据库引擎与会话（SQLAlchemy 2.0，同步驱动 PyMySQL）。

说明：BFF 选同步 ORM 而非 async，原因——
1. Celery worker 与 FastAPI 共用同一套模型代码，同步最省心；
2. PyMySQL 纯 Python 驱动，容器/Windows 本机零编译；
3. FastAPI 的 def 路由自动跑在线程池，同步 ORM 不阻塞事件循环。
"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_KWARGS: dict = {"echo": False}
if settings.DATABASE_URL.startswith("mysql"):
    # 连接池参数仅 MySQL 适用（SQLite/StaticPool 不接受 pool_size/max_overflow）
    _KWARGS.update(pool_pre_ping=True, pool_recycle=3600, pool_size=10, max_overflow=20)

engine = create_engine(settings.DATABASE_URL, **_KWARGS)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每请求一个 session，请求结束关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
