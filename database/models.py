from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, relationship
from datetime import datetime


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, nullable=False)  # Telegram user ID
    username = Column(String(100), nullable=False)
    email = Column(String(200))
    password = Column(String(200), nullable=False)
    proxy = Column(String(300))        # current active proxy
    proxy_list = Column(Text)          # JSON list of proxies for rotation
    proxy_index = Column(Integer, default=0)  # current position in proxy_list
    upload_count = Column(Integer, default=0) # total uploads done
    session_data = Column(Text)        # serialized browser cookies/storage
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    uploads = relationship("UploadTask", back_populates="account", cascade="all, delete-orphan")
    comment_tasks = relationship("CommentTask", back_populates="account", cascade="all, delete-orphan")


class UploadTask(Base):
    __tablename__ = "upload_tasks"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    owner_id = Column(Integer, nullable=False)
    video_path = Column(String(500), nullable=False)
    caption = Column(Text, default="")
    hashtags = Column(String(500), default="")
    privacy = Column(String(20), default="public")  # public / friends / private
    status = Column(String(20), default="pending")   # pending / running / done / failed
    error_message = Column(Text)
    scheduled_at = Column(DateTime)
    completed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("Account", back_populates="uploads")


class CommentTask(Base):
    __tablename__ = "comment_tasks"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    owner_id = Column(Integer, nullable=False)
    hashtag = Column(String(200), nullable=False)  # keyword/hashtag to search
    comment_text = Column(Text, nullable=False)
    count = Column(Integer, default=1)         # how many videos to comment on
    status = Column(String(20), default="pending")
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("Account", back_populates="comment_tasks")


class BotUser(Base):
    __tablename__ = "bot_users"

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, unique=True, nullable=False)
    username   = Column(String(100))
    first_name = Column(String(100))
    role       = Column(String(20), default="admin")
    added_by   = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)
