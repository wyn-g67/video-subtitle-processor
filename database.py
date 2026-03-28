# database.py
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, String, Integer, DateTime, JSON, Text
from datetime import datetime
import uuid

# ========== 共用模型 ==========
Base = declarative_base()

class Task(Base):
    __tablename__ = "tasks"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), nullable=True)
    video_path = Column(String(500), nullable=False)
    video_info = Column(JSON, nullable=True)
    subtitles = Column(JSON, nullable=True)
    style = Column(JSON, nullable=True)
    status = Column(String(20), default="pending")
    progress = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    output_path = Column(String(500), nullable=True)
    subtitle_path = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

# ========== 同步引擎（给 Celery Worker 用） ==========
SYNC_DATABASE_URL = "postgresql://video_user:ER34er34!@localhost/video_processor"
sync_engine = create_engine(SYNC_DATABASE_URL, echo=True)
SyncSessionLocal = sessionmaker(bind=sync_engine, autocommit=False, autoflush=False)

# ========== 异步引擎（给 FastAPI 用） ==========
ASYNC_DATABASE_URL = "postgresql+asyncpg://video_user:ER34er34!@localhost/video_processor"
async_engine = create_async_engine(ASYNC_DATABASE_URL, echo=True)
AsyncSessionLocal = async_sessionmaker(async_engine, expire_on_commit=False)

# ========== 初始化表 ==========
def init_db():
    """创建所有表（用同步引擎就够了）"""
    Base.metadata.create_all(bind=sync_engine)
async def get_db():
    """异步数据库会话（给 FastAPI 用）"""
    async with AsyncSessionLocal() as session:
        yield session
