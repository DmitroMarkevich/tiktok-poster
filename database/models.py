from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Text, ForeignKey, UniqueConstraint
)
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
    last_warmup_at = Column(DateTime)         # when this account was last warmed up
    warmup_count   = Column(Integer, default=0)  # total warmup sessions done

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


class CommentedVideo(Base):
    """Persistent per-account history of videos already commented — so an account
    never comments the same video twice, even across separate runs."""
    __tablename__ = "commented_videos"

    id         = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    video_id   = Column(String(64), nullable=False)   # numeric id from /video/<id>
    status     = Column(String(16), default="success")
    comment_text = Column(Text)                        # what was posted (for shadowban re-check)
    visible    = Column(Integer)                        # NULL=unchecked, 1=visible to guests, 0=shadowbanned
    verified_at = Column(DateTime)                      # when the shadowban check ran
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("account_id", "video_id", name="uq_acc_video"),)


class VideoClaim(Base):
    """Global cross-account registry: a video is claimed by ONE account per run so
    multiple accounts never pile onto the same video (a clear bot-cluster fingerprint
    that feeds the shadow-filter). A failed post releases the claim for another account."""
    __tablename__ = "video_claims"

    video_id   = Column(String(64), primary_key=True)
    account_id = Column(Integer, nullable=False)
    topic      = Column(String(120))
    status     = Column(String(16), default="pending")  # pending / success / failed
    claimed_at = Column(DateTime, default=datetime.utcnow)


class AutopilotState(Base):
    """Persisted autopilot config so a running autopilot survives a bot restart."""
    __tablename__ = "autopilot_state"

    owner_id     = Column(Integer, primary_key=True)
    chat_id      = Column(Integer)
    hashtag      = Column(String(200))
    comment_text = Column(Text)
    count        = Column(Integer, default=1)
    warmup_min   = Column(Integer, default=0)
    active       = Column(Boolean, default=False)
    updated_at   = Column(DateTime, default=datetime.utcnow)


class OutlookAccount(Base):
    """Outlook акаунти, створені авторегером — використовуються для реєстрації TikTok."""
    __tablename__ = "outlook_accounts"

    id         = Column(Integer, primary_key=True)
    owner_id   = Column(Integer, nullable=False)
    email      = Column(String(200), unique=True, nullable=False)
    password   = Column(String(200), nullable=False)
    first_name = Column(String(100))
    last_name  = Column(String(100))
    birth_year = Column(Integer)
    proxy      = Column(String(300))          # проксі, через який створено
    status     = Column(String(20), default="created")  # created / used / banned
    tiktok_id  = Column(Integer, ForeignKey("accounts.id"))  # якщо вже прив'язаний до TikTok акку
    created_at = Column(DateTime, default=datetime.utcnow)


class BotUser(Base):
    __tablename__ = "bot_users"

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, unique=True, nullable=False)
    username   = Column(String(100))
    first_name = Column(String(100))
    role       = Column(String(20), default="admin")
    added_by   = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)
