from aiogram.fsm.state import State, StatesGroup


class AddAccount(StatesGroup):
    username = State()
    cookies = State()


class UploadVideo(StatesGroup):
    choose_account = State()
    select_accounts = State()
    choose_media = State()  # after account scope: video vs photo carousel
    send_video = State()
    send_photos = State()   # photo-carousel mode: collect multiple images
    caption = State()
    hashtags = State()
    privacy = State()


class CommentTask(StatesGroup):
    select_accounts = State()
    hashtag = State()
    comment_text = State()
    count = State()


class Warmup(StatesGroup):
    select_accounts = State()
    topic = State()


class Autopilot(StatesGroup):
    hashtag = State()
    comment_text = State()
    count = State()


class ImportCookies(StatesGroup):
    waiting_cookies = State()


class ProxyManage(StatesGroup):
    waiting_proxy = State()
    waiting_proxy_list = State()
    waiting_bulk_file = State()


class CaptchaSettings(StatesGroup):
    waiting_api_key = State()


class AdminManage(StatesGroup):
    waiting_user_id = State()
