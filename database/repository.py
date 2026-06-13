from __future__ import annotations
import json
from datetime import datetime, date
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
from .models import Account, UploadTask, CommentTask, BotUser


class AccountRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, owner_id: int, username: str, password: str = "",
                  email: str = None, proxy: str = None,
                  session_data: str = None) -> Account:
        acc = Account(owner_id=owner_id, username=username,
                      password=password, email=email, proxy=proxy,
                      session_data=session_data)
        self.session.add(acc)
        await self.session.commit()
        await self.session.refresh(acc)
        return acc

    async def get_by_id(self, account_id: int) -> Account | None:
        result = await self.session.execute(
            select(Account).where(Account.id == account_id)
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, owner_id: int, email: str) -> Account | None:
        result = await self.session.execute(
            select(Account).where(Account.owner_id == owner_id, Account.email == email)
        )
        return result.scalar_one_or_none()

    async def list_by_owner(self, owner_id: int) -> list[Account]:
        result = await self.session.execute(
            select(Account).where(Account.owner_id == owner_id, Account.is_active == True)
        )
        return list(result.scalars().all())

    async def update_session(self, account_id: int, session_data: str):
        await self.session.execute(
            update(Account).where(Account.id == account_id).values(session_data=session_data)
        )
        await self.session.commit()

    async def rotate_proxy(self, account_id: int) -> str | None:
        acc = await self.get_by_id(account_id)
        if not acc or not acc.proxy_list:
            return acc.proxy if acc else None
        try:
            proxies = json.loads(acc.proxy_list)
        except Exception:
            return acc.proxy
        if not proxies:
            return acc.proxy
        new_index = (acc.proxy_index + 1) % len(proxies)
        new_proxy = proxies[new_index]
        await self.session.execute(
            update(Account).where(Account.id == account_id).values(
                proxy=new_proxy,
                proxy_index=new_index,
                upload_count=Account.upload_count + 1,
            )
        )
        await self.session.commit()
        return new_proxy

    async def set_proxy_list(self, account_id: int, proxies: list):
        first_proxy = proxies[0] if proxies else None
        await self.session.execute(
            update(Account).where(Account.id == account_id).values(
                proxy_list=json.dumps(proxies),
                proxy_index=0,
                proxy=first_proxy,
            )
        )
        await self.session.commit()

    async def delete(self, account_id: int):
        acc = await self.get_by_id(account_id)
        if acc:
            await self.session.delete(acc)
            await self.session.commit()


class UploadRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, account_id: int, owner_id: int, video_path: str,
                     caption: str, hashtags: str, privacy: str,
                     scheduled_at: datetime = None) -> UploadTask:
        task = UploadTask(
            account_id=account_id, owner_id=owner_id,
            video_path=video_path, caption=caption,
            hashtags=hashtags, privacy=privacy, scheduled_at=scheduled_at
        )
        self.session.add(task)
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def set_status(self, task_id: int, status: str, error: str = None):
        values = {"status": status}
        if status == "done":
            values["completed_at"] = datetime.utcnow()
        if error:
            values["error_message"] = error
        await self.session.execute(
            update(UploadTask).where(UploadTask.id == task_id).values(**values)
        )
        await self.session.commit()

    async def list_by_owner(self, owner_id: int) -> list[UploadTask]:
        result = await self.session.execute(
            select(UploadTask).where(UploadTask.owner_id == owner_id)
            .order_by(UploadTask.created_at.desc()).limit(20)
        )
        return list(result.scalars().all())

    async def get_uploads_today(self, owner_id: int) -> int:
        today_start = datetime.combine(date.today(), datetime.min.time())
        result = await self.session.execute(
            select(func.count(UploadTask.id)).where(
                UploadTask.owner_id == owner_id,
                UploadTask.created_at >= today_start,
            )
        )
        return result.scalar() or 0

    async def get_stats(self, owner_id: int) -> dict:
        today_start = datetime.combine(date.today(), datetime.min.time())

        total = (await self.session.execute(
            select(func.count(UploadTask.id)).where(UploadTask.owner_id == owner_id)
        )).scalar() or 0

        today = (await self.session.execute(
            select(func.count(UploadTask.id)).where(
                UploadTask.owner_id == owner_id,
                UploadTask.created_at >= today_start,
            )
        )).scalar() or 0

        success = (await self.session.execute(
            select(func.count(UploadTask.id)).where(
                UploadTask.owner_id == owner_id,
                UploadTask.status == "done",
            )
        )).scalar() or 0

        failed = (await self.session.execute(
            select(func.count(UploadTask.id)).where(
                UploadTask.owner_id == owner_id,
                UploadTask.status == "failed",
            )
        )).scalar() or 0

        return {"total": total, "today": today, "success": success, "failed": failed}


class CommentRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, account_id: int, owner_id: int,
                     hashtag: str, comment_text: str, count: int) -> CommentTask:
        task = CommentTask(
            account_id=account_id, owner_id=owner_id,
            hashtag=hashtag, comment_text=comment_text, count=count
        )
        self.session.add(task)
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def set_status(self, task_id: int, status: str, error: str = None):
        values = {"status": status}
        if error:
            values["error_message"] = error
        await self.session.execute(
            update(CommentTask).where(CommentTask.id == task_id).values(**values)
        )
        await self.session.commit()


class BotUserRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, user_id: int) -> BotUser | None:
        r = await self.session.execute(select(BotUser).where(BotUser.user_id == user_id))
        return r.scalar_one_or_none()

    async def add(self, user_id: int, username: str, first_name: str,
                  role: str, added_by: int) -> BotUser:
        u = BotUser(user_id=user_id, username=username, first_name=first_name,
                    role=role, added_by=added_by)
        self.session.add(u)
        await self.session.commit()
        await self.session.refresh(u)
        return u

    async def delete(self, user_id: int):
        u = await self.get(user_id)
        if u:
            await self.session.delete(u)
            await self.session.commit()

    async def list_admins(self) -> list[BotUser]:
        r = await self.session.execute(select(BotUser))
        return list(r.scalars().all())
