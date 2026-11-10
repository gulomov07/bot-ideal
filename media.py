import asyncio
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import html

from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import yt_dlp

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    ErrorEvent
)
from aiogram.exceptions import TelegramBadRequest


# =========================================================
# 1. ASOSIY SOZLAMALAR
# =========================================================

BOT_TOKEN = "8931868317:AAEbuJdyrHAXEMxvmAWpWtFVad4hIOH9utk"
AUDD_TOKEN = "9659a4792793dccff601709e17770da0"

# Admin Telegram ID raqamingni shu yerga yoz.
# 0 bo'lsa admin panel o'chirilgan bo'ladi.
ADMIN_ID = 0

BOT_NAME = "MEDIA MASTER PRO"
BOT_VERSION = "6.0 ULTRA"

DOWNLOAD_DIR = "downloads"
DB_FILE = "media_master.db"

# Telegramga yuborish uchun xavfsizroq hajm chegarasi.
MAX_FILE_SIZE = 49 * 1024 * 1024

# Bir vaqtda nechta yuklash ishlashi.
MAX_PARALLEL_DOWNLOADS = 2

# Yuklangan papkalarni necha soatdan keyin tozalash.
CLEANUP_AFTER_HOURS = 2

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("media_master.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()

download_semaphore = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)

# Oxirgi yuborilgan havolalar.
user_links = {}

# Faol yuklashlar.
active_jobs = {}

# Faol papkalar.
active_folders = set()


# =========================================================
# 2. DATABASE
# =========================================================

def connect_db():
    return sqlite3.connect(DB_FILE)


def init_db():
    con = connect_db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            downloads INTEGER DEFAULT 0,
            joined TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY,
            quality TEXT DEFAULT '720',
            bitrate TEXT DEFAULT '192'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            media_type TEXT,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, url)
        )
    """)

    con.commit()
    con.close()


def register_user(user):
    con = connect_db()
    cur = con.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO users
        (user_id, name, username)
        VALUES (?, ?, ?)
    """, (
        user.id,
        user.full_name,
        user.username
    ))

    cur.execute("""
        UPDATE users
        SET name=?, username=?
        WHERE user_id=?
    """, (
        user.full_name,
        user.username,
        user.id
    ))

    cur.execute("""
        INSERT OR IGNORE INTO settings (user_id)
        VALUES (?)
    """, (user.id,))

    con.commit()
    con.close()


def get_settings(user_id):
    con = connect_db()
    cur = con.cursor()

    cur.execute("""
        SELECT quality, bitrate
        FROM settings
        WHERE user_id=?
    """, (user_id,))

    result = cur.fetchone()
    con.close()

    return result if result else ("720", "192")


def update_setting(user_id, column, value):
    if column not in ("quality", "bitrate"):
        return

    con = connect_db()
    cur = con.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO settings (user_id)
        VALUES (?)
    """, (user_id,))

    cur.execute(
        f"UPDATE settings SET {column}=? WHERE user_id=?",
        (value, user_id)
    )

    con.commit()
    con.close()


def add_history(user_id, url, media_type):
    con = connect_db()
    cur = con.cursor()

    cur.execute("""
        INSERT INTO history (user_id, url, media_type)
        VALUES (?, ?, ?)
    """, (user_id, url, media_type))

    cur.execute("""
        UPDATE users
        SET downloads = downloads + 1
        WHERE user_id=?
    """, (user_id,))

    con.commit()
    con.close()


def get_history(user_id):
    con = connect_db()
    cur = con.cursor()

    cur.execute("""
        SELECT id, url, media_type, created
        FROM history
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 15
    """, (user_id,))

    result = cur.fetchall()
    con.close()

    return result


def clear_history(user_id):
    con = connect_db()
    cur = con.cursor()

    cur.execute(
        "DELETE FROM history WHERE user_id=?",
        (user_id,)
    )

    con.commit()
    con.close()


def add_favorite(user_id, url):
    con = connect_db()
    cur = con.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO favorites (user_id, url)
        VALUES (?, ?)
    """, (user_id, url))

    con.commit()
    con.close()


def get_favorites(user_id):
    con = connect_db()
    cur = con.cursor()

    cur.execute("""
        SELECT id, url
        FROM favorites
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 20
    """, (user_id,))

    result = cur.fetchall()
    con.close()

    return result


def delete_favorite(user_id, favorite_id):
    con = connect_db()
    cur = con.cursor()

    cur.execute("""
        DELETE FROM favorites
        WHERE id=? AND user_id=?
    """, (favorite_id, user_id))

    con.commit()
    con.close()


def get_user_downloads(user_id):
    con = connect_db()
    cur = con.cursor()

    cur.execute(
        "SELECT downloads FROM users WHERE user_id=?",
        (user_id,)
    )

    result = cur.fetchone()
    con.close()

    return result[0] if result else 0


def get_total_users():
    con = connect_db()
    cur = con.cursor()

    cur.execute("SELECT COUNT(*) FROM users")
    result = cur.fetchone()[0]

    con.close()
    return result


def get_total_downloads():
    con = connect_db()
    cur = con.cursor()

    cur.execute("SELECT SUM(downloads) FROM users")
    result = cur.fetchone()[0] or 0

    con.close()
    return result


# =========================================================
# 3. YORDAMCHI FUNKSIYALAR
# =========================================================

def safe_text(value):
    return html.escape(str(value or "Noma'lum"))


def get_url_from_text(text):
    match = re.search(r"https?://[^\s<>]+", text or "")

    if not match:
        return None

    return match.group(0).rstrip(".,!?)]}")


ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "pinterest.com",
    "pin.it"
)


def valid_url(url):
    try:
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return False

        host = (parsed.hostname or "").lower()

        return any(
            host == domain or host.endswith("." + domain)
            for domain in ALLOWED_DOMAINS
        )

    except Exception:
        return False


def make_folder(user_id):
    folder = os.path.join(
        DOWNLOAD_DIR,
        f"{user_id}_{uuid.uuid4().hex[:10]}"
    )

    os.makedirs(folder, exist_ok=True)
    return folder


def find_downloaded_file(folder, prefix):
    files = []

    for path in Path(folder).glob(prefix + ".*"):
        if not path.is_file():
            continue

        if path.name.endswith((".part", ".ytdl", ".tmp")):
            continue

        files.append(path)

    if not files:
        return None

    files.sort(
        key=lambda item: item.stat().st_size,
        reverse=True
    )

    return str(files[0])


def check_cancel(cancel_event):
    if cancel_event.is_set():
        raise yt_dlp.utils.DownloadError(
            "Yuklash foydalanuvchi tomonidan bekor qilindi."
        )


def download_progress_hook(cancel_event):
    def hook(data):
        check_cancel(cancel_event)

    return hook


async def safe_edit(message, text, reply_markup=None):
    try:
        await message.edit_text(
            text,
            reply_markup=reply_markup
        )
    except TelegramBadRequest:
        try:
            await message.answer(
                text,
                reply_markup=reply_markup
            )
        except Exception:
            pass
    except Exception:
        pass


# =========================================================
# 4. VIDEO YUKLASH
# =========================================================

def download_video_sync(url, folder, quality, cancel_event):
    check_cancel(cancel_event)

    output = os.path.join(folder, "video.%(ext)s")

    if quality == "best":
        format_value = "bestvideo+bestaudio/best"
    else:
        format_value = (
            f"bestvideo[height<={quality}]+bestaudio/"
            f"best[height<={quality}]/best"
        )

    options = {
        "format": format_value,
        "outtmpl": output,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "overwrites": True,
        "progress_hooks": [
            download_progress_hook(cancel_event)
        ]
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    check_cancel(cancel_event)

    return find_downloaded_file(folder, "video")


# =========================================================
# 5. MP3 YUKLASH
# =========================================================

def download_audio_sync(url, folder, bitrate, cancel_event):
    check_cancel(cancel_event)

    output = os.path.join(folder, "audio.%(ext)s")

    options = {
        "format": "bestaudio/best",
        "outtmpl": output,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
        "overwrites": True,
        "progress_hooks": [
            download_progress_hook(cancel_event)
        ],
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(bitrate)
            }
        ]
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    check_cancel(cancel_event)

    return find_downloaded_file(folder, "audio")


# =========================================================
# 6. MEDIA MA'LUMOTLARI
# =========================================================

def get_media_info_sync(url):
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 25
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


# =========================================================
# 7. AUDD MUSIC FINDER
# =========================================================

def create_music_sample(audio_path, sample_path):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg o'rnatilmagan.")

    command = [
        "ffmpeg",
        "-y",
        "-i", audio_path,
        "-t", "20",
        "-vn",
        "-ac", "1",
        "-b:a", "128k",
        sample_path
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60
    )

    if result.returncode != 0 or not os.path.exists(sample_path):
        raise RuntimeError("Audio namunasi tayyorlanmadi.")

    return sample_path


async def recognize_music(audio_path, folder):
    if not AUDD_TOKEN or "BU_YERGA" in AUDD_TOKEN:
        return None

    sample_path = os.path.join(folder, "music_sample.mp3")

    await asyncio.to_thread(
        create_music_sample,
        audio_path,
        sample_path
    )

    api_url = "https://api.audd.io/"
    timeout = aiohttp.ClientTimeout(total=90)

    form = aiohttp.FormData()
    form.add_field("api_token", AUDD_TOKEN)
    form.add_field("return", "spotify,apple_music")

    with open(sample_path, "rb") as audio:
        form.add_field(
            "file",
            audio,
            filename="sample.mp3",
            content_type="audio/mpeg"
        )

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(api_url, data=form) as response:
                if response.status != 200:
                    return None

                data = await response.json()

    if data.get("status") == "success":
        return data.get("result")

    return None


# =========================================================
# 8. ASOSIY MENYU
# =========================================================

def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 Video yuklash",
                    callback_data="download_video"
                ),
                InlineKeyboardButton(
                    text="🎵 MP3 yuklash",
                    callback_data="download_mp3"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔎 Music Finder",
                    callback_data="music_finder"
                ),
                InlineKeyboardButton(
                    text="🖼 Preview",
                    callback_data="preview"
                )
            ],
            [
                InlineKeyboardButton(
                    text="ℹ️ Media Analyzer",
                    callback_data="media_info"
                ),
                InlineKeyboardButton(
                    text="⚡ Quick Download",
                    callback_data="quick"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Sevimlilar",
                    callback_data="favorites"
                ),
                InlineKeyboardButton(
                    text="🕘 Tarix",
                    callback_data="history"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👤 Profil",
                    callback_data="profile"
                ),
                InlineKeyboardButton(
                    text="⚙️ Sozlamalar",
                    callback_data="settings"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Statistika",
                    callback_data="stats"
                ),
                InlineKeyboardButton(
                    text="🌐 Platformalar",
                    callback_data="platforms"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❓ Yordam",
                    callback_data="help"
                ),
                InlineKeyboardButton(
                    text="🤖 Bot haqida",
                    callback_data="about"
                )
            ]
        ]
    )


def back_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏠 Bosh menyu",
                    callback_data="home"
                )
            ]
        ]
    )


def after_download_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 Faqat video",
                    callback_data="download_video"
                ),
                InlineKeyboardButton(
                    text="🎵 Faqat MP3",
                    callback_data="download_mp3"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔎 Musiqani aniqlash",
                    callback_data="music_finder"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Sevimliga qo'shish",
                    callback_data="save_favorite"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Bosh menyu",
                    callback_data="home"
                ),
                InlineKeyboardButton(
                    text="🔗 Yangi link",
                    callback_data="new_link"
                )
            ]
        ]
    )


# =========================================================
# 9. SOZLAMALAR KEYBOARDLARI
# =========================================================

def settings_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎞 Video sifati",
                    callback_data="quality_menu"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎵 MP3 sifati",
                    callback_data="bitrate_menu"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Bosh menyu",
                    callback_data="home"
                )
            ]
        ]
    )


def quality_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="360p",
                    callback_data="quality_360"
                ),
                InlineKeyboardButton(
                    text="480p",
                    callback_data="quality_480"
                )
            ],
            [
                InlineKeyboardButton(
                    text="720p",
                    callback_data="quality_720"
                ),
                InlineKeyboardButton(
                    text="1080p",
                    callback_data="quality_1080"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏆 Best Quality",
                    callback_data="quality_best"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Sozlamalar",
                    callback_data="settings"
                )
            ]
        ]
    )


def bitrate_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="128 kbps",
                    callback_data="bitrate_128"
                ),
                InlineKeyboardButton(
                    text="192 kbps",
                    callback_data="bitrate_192"
                )
            ],
            [
                InlineKeyboardButton(
                    text="256 kbps",
                    callback_data="bitrate_256"
                ),
                InlineKeyboardButton(
                    text="320 kbps",
                    callback_data="bitrate_320"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Sozlamalar",
                    callback_data="settings"
                )
            ]
        ]
    )


# =========================================================
# 10. START VA MENU
# =========================================================

@dp.message(CommandStart())
async def start(message: Message):
    register_user(message.from_user)

    name = safe_text(message.from_user.first_name)

    text = f"""
<b>✨ MEDIA MASTER PRO {BOT_VERSION}</b>

Assalomu alaykum, {name}!

🎬 Video yuklash
🎵 MP3 konvertatsiya
🔎 Music Finder
🖼 Video Preview
ℹ️ Media Analyzer
⭐ Sevimlilar
📥 Yuklashlar tarixi

<b>Kerakli xizmatni tanlang:</b>
"""

    await message.answer(
        text,
        reply_markup=main_keyboard()
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "<b>MEDIA MASTER PRO — YORDAM</b>\n\n"
        "1. YouTube, Instagram, TikTok yoki Pinterest havolasini yuboring.\n"
        "2. Bot videoni va MP3 audioni tayyorlaydi.\n"
        "3. Sifatni Sozlamalar bo'limidan tanlang.\n"
        "4. Music Finder orqali qo'shiqni aniqlang.\n\n"
        "Faqat foydalanishga ruxsatingiz bo'lgan kontentni yuklang.",
        reply_markup=main_keyboard()
    )


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    await callback.answer()

    await safe_edit(
        callback.message,
        "<b>✨ MEDIA MASTER PRO 6.0 ULTRA</b>\n\n"
        "Kerakli bo'limni tanlang:",
        main_keyboard()
    )


# =========================================================
# 11. ASOSIY YUKLASH FUNKSIYASI
# =========================================================

async def process_media(
    message: Message,
    url: str,
    send_video=True,
    send_audio=True
):
    user_id = message.from_user.id

    if user_id in active_jobs:
        await message.answer(
            "⏳ Sizda hozir boshqa yuklash jarayoni bor.\n"
            "Avval uni tugating yoki bekor qiling."
        )
        return

    folder = make_folder(user_id)
    cancel_event = threading.Event()

    active_folders.add(folder)

    status = await message.answer(
        "⏳ <b>MEDIA MASTER PRO</b>\n\n"
        "📥 Yuklash navbatiga qo'shildi...",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Bekor qilish",
                        callback_data="cancel_download"
                    )
                ]
            ]
        )
    )

    active_jobs[user_id] = {
        "event": cancel_event,
        "folder": folder,
        "status": status
    }

    sent_any = False

    try:
        async with download_semaphore:
            if cancel_event.is_set():
                raise asyncio.CancelledError

            quality, bitrate = get_settings(user_id)

            if send_video:
                await safe_edit(
                    status,
                    "🎬 <b>VIDEO YUKLANMOQDA...</b>\n\n"
                    f"📺 Sifat: {quality}\n"
                    "⏳ Iltimos, kuting."
                )

                video_path = await asyncio.to_thread(
                    download_video_sync,
                    url,
                    folder,
                    quality,
                    cancel_event
                )

                if cancel_event.is_set():
                    raise asyncio.CancelledError

                if not video_path:
                    raise RuntimeError("Video fayli topilmadi.")

                size = os.path.getsize(video_path)

                if size > MAX_FILE_SIZE:
                    await message.answer(
                        "⚠️ Video Telegram orqali yuborish uchun juda katta."
                    )
                else:
                    await message.answer_video(
                        FSInputFile(video_path),
                        caption=(
                            "🎬 <b>MEDIA MASTER PRO</b>\n"
                            f"📺 Sifat: {quality}"
                        )
                    )
                    sent_any = True

            if send_audio:
                if cancel_event.is_set():
                    raise asyncio.CancelledError

                await safe_edit(
                    status,
                    "🎵 <b>MP3 TAYYORLANMOQDA...</b>\n\n"
                    f"🎚 Sifat: {bitrate} kbps"
                )

                audio_path = await asyncio.to_thread(
                    download_audio_sync,
                    url,
                    folder,
                    bitrate,
                    cancel_event
                )

                if cancel_event.is_set():
                    raise asyncio.CancelledError

                if not audio_path:
                    raise RuntimeError("MP3 fayli topilmadi.")

                size = os.path.getsize(audio_path)

                if size > MAX_FILE_SIZE:
                    await message.answer(
                        "⚠️ MP3 fayli Telegram yuborish limitidan katta."
                    )
                else:
                    await message.answer_audio(
                        FSInputFile(audio_path),
                        caption=(
                            "🎵 <b>MEDIA MASTER PRO</b>\n"
                            f"🎚 Sifat: {bitrate} kbps"
                        )
                    )
                    sent_any = True

        if sent_any:
            media_type = "Video + MP3" if send_video and send_audio else (
                "Video" if send_video else "MP3"
            )

            add_history(user_id, url, media_type)

            await safe_edit(
                status,
                "✅ <b>YUKLASH MUVAFFAQIYATLI YAKUNLANDI!</b>\n\n"
                "✨ Yana biror xizmatni tanlang:",
                after_download_keyboard()
            )
        else:
            await safe_edit(
                status,
                "⚠️ Fayl yuborilmadi. Hajmini yoki havolani tekshiring.",
                after_download_keyboard()
            )

    except asyncio.CancelledError:
        await safe_edit(
            status,
            "❌ <b>YUKLASH BEKOR QILINDI.</b>",
            after_download_keyboard()
        )

    except Exception as error:
        logging.exception("Media yuklash xatosi")

        if cancel_event.is_set():
            text = "❌ Yuklash bekor qilindi."
        else:
            text = (
                "❌ <b>YUKLASHDA XATOLIK!</b>\n\n"
                "Havolani tekshiring yoki keyinroq urinib ko'ring.\n"
                "Agar xato takrorlansa, administratorga murojaat qiling."
            )

        await safe_edit(
            status,
            text,
            after_download_keyboard()
        )

    finally:
        active_jobs.pop(user_id, None)
        active_folders.discard(folder)
        shutil.rmtree(folder, ignore_errors=True)


# =========================================================
# 12. LINK QABUL QILISH
# =========================================================

@dp.message(F.text)
async def receive_link(message: Message):
    register_user(message.from_user)

    url = get_url_from_text(message.text)

    if not url or not valid_url(url):
        await message.answer(
            "🔗 <b>Havola topilmadi yoki platforma qo'llab-quvvatlanmaydi.</b>\n\n"
            "Quyidagi platformalar havolasini yuboring:\n"
            "▶️ YouTube\n"
            "📸 Instagram\n"
            "🎵 TikTok\n"
            "📌 Pinterest",
            reply_markup=main_keyboard()
        )
        return

    user_links[message.from_user.id] = url

    await process_media(
        message,
        url,
        send_video=True,
        send_audio=True
    )


# =========================================================
# 13. VIDEO VA MP3 TUGMALARI
# =========================================================

@dp.callback_query(F.data == "download_video")
async def download_video_button(callback: CallbackQuery):
    await callback.answer()

    url = user_links.get(callback.from_user.id)

    if not url:
        await callback.message.answer(
            "🔗 Avval video havolasini yuboring."
        )
        return

    await process_media(
        callback.message,
        url,
        send_video=True,
        send_audio=False
    )


@dp.callback_query(F.data == "download_mp3")
async def download_mp3_button(callback: CallbackQuery):
    await callback.answer()

    url = user_links.get(callback.from_user.id)

    if not url:
        await callback.message.answer(
            "🔗 Avval video havolasini yuboring."
        )
        return

    await process_media(
        callback.message,
        url,
        send_video=False,
        send_audio=True
    )


@dp.callback_query(F.data == "quick")
async def quick_download(callback: CallbackQuery):
    await callback.answer()

    url = user_links.get(callback.from_user.id)

    if not url:
        await callback.message.answer(
            "🔗 Tezkor yuklash uchun avval havola yuboring."
        )
        return

    await process_media(
        callback.message,
        url,
        send_video=True,
        send_audio=False
    )


# =========================================================
# 14. BEKOR QILISH
# =========================================================

@dp.callback_query(F.data == "cancel_download")
async def cancel_download(callback: CallbackQuery):
    job = active_jobs.get(callback.from_user.id)

    if not job:
        await callback.answer(
            "Faol yuklash topilmadi.",
            show_alert=True
        )
        return

    job["event"].set()

    await callback.answer(
        "Bekor qilish so'rovi yuborildi."
    )


@dp.message(Command("cancel"))
async def cancel_command(message: Message):
    job = active_jobs.get(message.from_user.id)

    if not job:
        await message.answer("Hozir faol yuklash yo'q.")
        return

    job["event"].set()

    await message.answer(
        "❌ Yuklashni bekor qilish so'rovi yuborildi."
    )


# =========================================================
# 15. PREVIEW
# =========================================================

@dp.callback_query(F.data == "preview")
async def preview(callback: CallbackQuery):
    await callback.answer()

    url = user_links.get(callback.from_user.id)

    if not url:
        await callback.message.answer(
            "🖼 Preview uchun avval havola yuboring."
        )
        return

    status = await callback.message.answer(
        "🖼 <b>VIDEO PREVIEW TAYYORLANMOQDA...</b>"
    )

    try:
        info = await asyncio.to_thread(
            get_media_info_sync,
            url
        )

        title = safe_text(info.get("title"))
        thumbnail = info.get("thumbnail")

        if thumbnail:
            await callback.message.answer_photo(
                photo=thumbnail,
                caption=f"🖼 <b>{title}</b>"
            )
        else:
            await callback.message.answer(
                f"🎬 {title}\n\nPreview mavjud emas."
            )

        await status.delete()

    except Exception:
        logging.exception("Preview xatosi")

        await safe_edit(
            status,
            "❌ Preview olish imkoni bo'lmadi.",
            back_keyboard()
        )


# =========================================================
# 16. MEDIA ANALYZER
# =========================================================

@dp.callback_query(F.data == "media_info")
async def media_info(callback: CallbackQuery):
    await callback.answer()

    url = user_links.get(callback.from_user.id)

    if not url:
        await callback.message.answer(
            "ℹ️ Avval media havolasini yuboring."
        )
        return

    status = await callback.message.answer(
        "🔍 <b>MEDIA ANALYZER ISHLAMOQDA...</b>"
    )

    try:
        info = await asyncio.to_thread(
            get_media_info_sync,
            url
        )

        title = safe_text(info.get("title"))
        uploader = safe_text(info.get("uploader"))
        duration = info.get("duration") or 0
        views = info.get("view_count") or 0
        likes = info.get("like_count") or 0
        width = info.get("width") or 0
        height = info.get("height") or 0
        fps = info.get("fps") or 0
        filesize = info.get("filesize") or info.get("filesize_approx") or 0

        if duration:
            minutes = int(duration) // 60
            seconds = int(duration) % 60
            duration_text = f"{minutes}:{seconds:02d}"
        else:
            duration_text = "Noma'lum"

        size_text = (
            f"{filesize / (1024 * 1024):.2f} MB"
            if filesize else "Noma'lum"
        )

        platform = safe_text(urlparse(url).hostname)

        text = f"""
<b>ℹ️ MEDIA ANALYZER</b>

🎬 <b>Nomi:</b> {title}

👤 <b>Muallif:</b> {uploader}

🌐 <b>Platforma:</b> {platform}

⏱ <b>Davomiyligi:</b> {duration_text}

👁 <b>Ko'rishlar:</b> {views:,}

❤️ <b>Layklar:</b> {likes:,}

📺 <b>O'lcham:</b> {width} × {height}

🎞 <b>FPS:</b> {fps}

💾 <b>Hajmi:</b> {size_text}
"""

        await safe_edit(
            status,
            text,
            back_keyboard()
        )

    except Exception:
        logging.exception("Media analyzer xatosi")

        await safe_edit(
            status,
            "❌ Media ma'lumotlarini olish imkoni bo'lmadi.",
            back_keyboard()
        )


# =========================================================
# 17. MUSIC FINDER
# =========================================================

@dp.callback_query(F.data == "music_finder")
async def music_finder(callback: CallbackQuery):
    await callback.answer()

    url = user_links.get(callback.from_user.id)

    if not url:
        await callback.message.answer(
            "🔗 Musiqani aniqlash uchun avval video havolasini yuboring."
        )
        return

    if not AUDD_TOKEN or "BU_YERGA" in AUDD_TOKEN:
        await callback.message.answer(
            "⚠️ AudD token kiritilmagan.\n"
            "AUDD_TOKEN qatoriga API tokeningizni yozing."
        )
        return

    user_id = callback.from_user.id

    if user_id in active_jobs:
        await callback.message.answer(
            "⏳ Avval davom etayotgan yuklashni tugating."
        )
        return

    folder = make_folder(user_id)
    cancel_event = threading.Event()
    active_folders.add(folder)

    status = await callback.message.answer(
        "🔎 <b>MUSIC FINDER</b>\n\n"
        "🎵 Audio olinmoqda..."
    )

    active_jobs[user_id] = {
        "event": cancel_event,
        "folder": folder,
        "status": status
    }

    try:
        async with download_semaphore:
            _, bitrate = get_settings(user_id)

            audio_path = await asyncio.to_thread(
                download_audio_sync,
                url,
                folder,
                bitrate,
                cancel_event
            )

            if cancel_event.is_set():
                raise asyncio.CancelledError

            if not audio_path:
                raise RuntimeError("Audio topilmadi.")

            await safe_edit(
                status,
                "🔍 <b>AudD orqali musiqa aniqlanmoqda...</b>"
            )

            result = await recognize_music(audio_path, folder)

            if not result:
                await safe_edit(
                    status,
                    "❌ Musiqa aniqlanmadi.\n"
                    "Boshqa video bilan urinib ko'ring.",
                    back_keyboard()
                )
                return

            title = safe_text(result.get("title"))
            artist = safe_text(result.get("artist"))
            album = safe_text(result.get("album"))

            text = f"""
<b>🎵 MUSIC FINDER NATIJASI</b>

🎧 <b>Qo'shiq:</b> {title}

👤 <b>Ijrochi:</b> {artist}

💿 <b>Albom:</b> {album}
"""

            buttons = []

            spotify = result.get("spotify") or {}
            apple = result.get("apple_music") or {}

            spotify_url = (
                spotify.get("external_urls", {}).get("spotify")
            )

            apple_url = apple.get("url")

            if spotify_url:
                buttons.append([
                    InlineKeyboardButton(
                        text="🎧 Spotify",
                        url=spotify_url
                    )
                ])

            if apple_url:
                buttons.append([
                    InlineKeyboardButton(
                        text="🍎 Apple Music",
                        url=apple_url
                    )
                ])

            buttons.append([
                InlineKeyboardButton(
                    text="🏠 Bosh menyu",
                    callback_data="home"
                )
            ])

            await safe_edit(
                status,
                text,
                InlineKeyboardMarkup(inline_keyboard=buttons)
            )

    except asyncio.CancelledError:
        await safe_edit(
            status,
            "❌ Music Finder bekor qilindi.",
            back_keyboard()
        )

    except Exception:
        logging.exception("Music Finder xatosi")

        await safe_edit(
            status,
            "❌ Musiqani aniqlashda xatolik yuz berdi.",
            back_keyboard()
        )

    finally:
        active_jobs.pop(user_id, None)
        active_folders.discard(folder)
        shutil.rmtree(folder, ignore_errors=True)


# =========================================================
# 18. SEVIMLILAR
# =========================================================

@dp.callback_query(F.data == "save_favorite")
async def save_favorite(callback: CallbackQuery):
    await callback.answer()

    url = user_links.get(callback.from_user.id)

    if not url:
        await callback.message.answer(
            "Avval havola yuboring."
        )
        return

    add_favorite(callback.from_user.id, url)

    await callback.message.answer(
        "⭐ Havola sevimlilarga saqlandi."
    )


@dp.callback_query(F.data == "favorites")
async def favorites(callback: CallbackQuery):
    await callback.answer()

    items = get_favorites(callback.from_user.id)

    if not items:
        await safe_edit(
            callback.message,
            "⭐ <b>SEVIMLILAR</b>\n\n"
            "Hozircha saqlangan havolalar yo'q.",
            back_keyboard()
        )
        return

    buttons = []
    text = "<b>⭐ SEVIMLI HAVOLALAR</b>\n\n"

    for index, (favorite_id, url) in enumerate(items, 1):
        text += f"{index}. {safe_text(url)}\n\n"

        buttons.append([
            InlineKeyboardButton(
                text=f"🗑️ {index}-havolani o'chirish",
                callback_data=f"favdel:{favorite_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="🏠 Bosh menyu",
            callback_data="home"
        )
    ])

    await safe_edit(
        callback.message,
        text[:4000],
        InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("favdel:"))
async def delete_favorite_button(callback: CallbackQuery):
    try:
        favorite_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Noto'g'ri so'rov.", show_alert=True)
        return

    delete_favorite(callback.from_user.id, favorite_id)

    await callback.answer("Sevimlidan o'chirildi.")

    items = get_favorites(callback.from_user.id)

    if not items:
        await safe_edit(
            callback.message,
            "⭐ Sevimlilar ro'yxati bo'sh.",
            back_keyboard()
        )
        return

    buttons = []
    text = "<b>⭐ SEVIMLI HAVOLALAR</b>\n\n"

    for index, (item_id, url) in enumerate(items, 1):
        text += f"{index}. {safe_text(url)}\n\n"

        buttons.append([
            InlineKeyboardButton(
                text=f"🗑️ {index}-havolani o'chirish",
                callback_data=f"favdel:{item_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="🏠 Bosh menyu",
            callback_data="home"
        )
    ])

    await safe_edit(
        callback.message,
        text[:4000],
        InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# =========================================================
# 19. TARIX
# =========================================================

@dp.callback_query(F.data == "history")
async def history(callback: CallbackQuery):
    await callback.answer()

    items = get_history(callback.from_user.id)

    if not items:
        await safe_edit(
            callback.message,
            "🕘 <b>YUKLASH TARIXI</b>\n\n"
            "Tarix hozircha bo'sh.",
            back_keyboard()
        )
        return

    text = "<b>🕘 OXIRGI YUKLASHLAR</b>\n\n"

    for index, item in enumerate(items, 1):
        _, url, media_type, created = item

        text += (
            f"<b>{index}.</b> {safe_text(media_type)}\n"
            f"🔗 {safe_text(url)}\n"
            f"📅 {safe_text(created)}\n\n"
        )

    buttons = [
        [
            InlineKeyboardButton(
                text="🗑️ Tarixni tozalash",
                callback_data="clear_history"
            )
        ],
        [
            InlineKeyboardButton(
                text="🏠 Bosh menyu",
                callback_data="home"
            )
        ]
    ]

    await safe_edit(
        callback.message,
        text[:4000],
        InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data == "clear_history")
async def clear_history_button(callback: CallbackQuery):
    clear_history(callback.from_user.id)

    await callback.answer("Tarix tozalandi!")

    await safe_edit(
        callback.message,
        "🗑️ <b>Yuklash tarixi tozalandi.</b>",
        back_keyboard()
    )


# =========================================================
# 20. PROFIL
# =========================================================

@dp.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):
    await callback.answer()

    user = callback.from_user
    downloads = get_user_downloads(user.id)
    quality, bitrate = get_settings(user.id)

    username = (
        "@" + safe_text(user.username)
        if user.username else "Mavjud emas"
    )

    text = f"""
<b>👤 SHAXSIY PROFIL</b>

🆔 <b>ID:</b> <code>{user.id}</code>

👤 <b>Ism:</b> {safe_text(user.full_name)}

🌐 <b>Username:</b> {username}

📥 <b>Yuklashlar:</b> {downloads}

🎬 <b>Video sifati:</b> {quality}

🎵 <b>MP3 sifati:</b> {bitrate} kbps

✨ <b>{BOT_NAME}</b>
"""

    await safe_edit(
        callback.message,
        text,
        back_keyboard()
    )


# =========================================================
# 21. SOZLAMALAR
# =========================================================

@dp.callback_query(F.data == "settings")
async def settings(callback: CallbackQuery):
    await callback.answer()

    quality, bitrate = get_settings(callback.from_user.id)

    text = f"""
<b>⚙️ SHAXSIY SOZLAMALAR</b>

🎬 Video sifati: <b>{quality}</b>

🎵 MP3 sifati: <b>{bitrate} kbps</b>

Quyidagi tugmalardan foydalaning.
"""

    await safe_edit(
        callback.message,
        text,
        settings_keyboard()
    )


@dp.callback_query(F.data == "quality_menu")
async def quality_menu(callback: CallbackQuery):
    await callback.answer()

    await safe_edit(
        callback.message,
        "🎞 <b>VIDEO SIFATINI TANLANG</b>",
        quality_keyboard()
    )


@dp.callback_query(F.data.startswith("quality_"))
async def set_quality(callback: CallbackQuery):
    quality = callback.data.replace("quality_", "")

    if quality not in ("360", "480", "720", "1080", "best"):
        await callback.answer(
            "Noto'g'ri sifat.",
            show_alert=True
        )
        return

    update_setting(
        callback.from_user.id,
        "quality",
        quality
    )

    await callback.answer("Video sifati saqlandi!")

    await settings(callback)


@dp.callback_query(F.data == "bitrate_menu")
async def bitrate_menu(callback: CallbackQuery):
    await callback.answer()

    await safe_edit(
        callback.message,
        "🎵 <b>MP3 SIFATINI TANLANG</b>",
        bitrate_keyboard()
    )


@dp.callback_query(F.data.startswith("bitrate_"))
async def set_bitrate(callback: CallbackQuery):
    bitrate = callback.data.replace("bitrate_", "")

    if bitrate not in ("128", "192", "256", "320"):
        await callback.answer(
            "Noto'g'ri bitrate.",
            show_alert=True
        )
        return

    update_setting(
        callback.from_user.id,
        "bitrate",
        bitrate
    )

    await callback.answer("MP3 sifati saqlandi!")

    await settings(callback)


# =========================================================
# 22. STATISTIKA
# =========================================================

@dp.callback_query(F.data == "stats")
async def stats(callback: CallbackQuery):
    await callback.answer()

    user_downloads = get_user_downloads(callback.from_user.id)

    text = f"""
<b>📊 SIZNING STATISTIKANGIZ</b>

📥 Yuklagan fayllaringiz: <b>{user_downloads}</b>

⚡ Bot holati: <b>Online</b>

🤖 Versiya: <b>{BOT_VERSION}</b>
"""

    await safe_edit(
        callback.message,
        text,
        back_keyboard()
    )


# =========================================================
# 23. PLATFORМALAR
# =========================================================

@dp.callback_query(F.data == "platforms")
async def platforms(callback: CallbackQuery):
    await callback.answer()

    text = """
<b>🌐 QO'LLAB-QUVVATLANADIGAN PLATFORМALAR</b>

▶️ YouTube

📸 Instagram

🎵 TikTok

📌 Pinterest

<b>Qanday ishlatish kerak?</b>

1. Platformadan ruxsat berilgan media havolasini oling.
2. Botga yuboring.
3. Bot media ma'lumotlarini tekshiradi.
4. Video va audio tayyorlanadi.

⚠️ Ayrim havolalar platforma cheklovlari sabab ishlamasligi mumkin.
"""

    await safe_edit(
        callback.message,
        text,
        back_keyboard()
    )


# =========================================================
# 24. YORDAM VA BOT HAQIDA
# =========================================================

@dp.callback_query(F.data == "help")
async def help_menu(callback: CallbackQuery):
    await callback.answer()

    text = """
<b>❓ YORDAM MARKAZI</b>

🔗 <b>Link yuborish:</b>
YouTube, Instagram, TikTok yoki Pinterest havolasini yuboring.

🎬 <b>Video:</b>
Video yuklash tugmasidan foydalaning.

🎵 <b>MP3:</b>
Audio formatida olish uchun MP3 tugmasini bosing.

🔎 <b>Music Finder:</b>
AudD yordamida qo'shiqni aniqlaydi.

⚙️ <b>Sozlamalar:</b>
Video va audio sifatini tanlang.

❌ <b>Bekor qilish:</b>
Faol yuklashni to'xtatish uchun tugmani bosing.

Faqat yuklashga ruxsatingiz bo'lgan kontentdan foydalaning.
"""

    await safe_edit(
        callback.message,
        text,
        back_keyboard()
    )


@dp.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    await callback.answer()

    text = f"""
<b>✨ {BOT_NAME}</b>

🚀 Professional media yordamchi

<b>Versiya:</b> {BOT_VERSION}

🎬 Video Downloader

🎵 MP3 Converter

🔎 Music Finder

🖼 Preview

ℹ️ Media Analyzer

⭐ Favorites

🕘 History

⚙️ Personal Settings

📊 Statistics

Botdan foydalanganingiz uchun rahmat!
"""

    await safe_edit(
        callback.message,
        text,
        back_keyboard()
    )


# =========================================================
# 25. YANGI LINK
# =========================================================

@dp.callback_query(F.data == "new_link")
async def new_link(callback: CallbackQuery):
    await callback.answer()

    await callback.message.answer(
        "🔗 <b>Yangi media havolasini yuboring.</b>"
    )


# =========================================================
# 26. ADMIN PANEL
# =========================================================

def admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Umumiy statistika",
                    callback_data="admin_stats"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Bosh menyu",
                    callback_data="home"
                )
            ]
        ]
    )


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Bu bo'lim faqat admin uchun.")
        return

    await message.answer(
        "<b>🛠 ADMIN PANEL</b>\n\n"
        "Kerakli bo'limni tanlang:",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not ADMIN_ID or callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Ruxsat yo'q.",
            show_alert=True
        )
        return

    await callback.answer()

    users = get_total_users()
    downloads = get_total_downloads()
    active = len(active_jobs)

    text = f"""
<b>📊 ADMIN STATISTIKA</b>

👥 Jami foydalanuvchilar: {users}

📥 Jami muvaffaqiyatli yuklashlar: {downloads}

⚙️ Faol jarayonlar: {active}

🟢 Bot: Online
"""

    await safe_edit(
        callback.message,
        text,
        admin_keyboard()
    )


# =========================================================
# 27. TELEGRAMGA YUBORILGAN AUDIO/VIDEO
# =========================================================

@dp.message(F.video | F.audio)
async def receive_telegram_media(message: Message):
    register_user(message.from_user)

    media = message.video or message.audio

    if not media:
        return

    if media.file_size and media.file_size > MAX_FILE_SIZE:
        await message.answer(
            "⚠️ Fayl juda katta. Kichikroq fayl yuboring."
        )
        return

    if not AUDD_TOKEN or "BU_YERGA" in AUDD_TOKEN:
        await message.answer(
            "🎵 Fayl qabul qilindi.\n"
            "Musiqani aniqlash uchun AudD token kerak."
        )
        return

    folder = make_folder(message.from_user.id)
    active_folders.add(folder)

    status = await message.answer(
        "🔎 <b>Yuborilgan fayldagi musiqa aniqlanmoqda...</b>"
    )

    try:
        file = await bot.get_file(media.file_id)

        extension = ".mp3" if message.audio else ".mp4"
        source_path = os.path.join(folder, "source" + extension)

        await bot.download_file(
            file.file_path,
            destination=source_path
        )

        result = await recognize_music(source_path, folder)

        if not result:
            await safe_edit(
                status,
                "❌ Musiqa aniqlanmadi.",
                back_keyboard()
            )
            return

        title = safe_text(result.get("title"))
        artist = safe_text(result.get("artist"))
        album = safe_text(result.get("album"))

        await safe_edit(
            status,
            f"""
<b>🎵 MUSIC FINDER</b>

🎧 Qo'shiq: {title}

👤 Ijrochi: {artist}

💿 Albom: {album}
""",
            back_keyboard()
        )

    except Exception:
        logging.exception("Telegram media tahlili xatosi")

        await safe_edit(
            status,
            "❌ Faylni tahlil qilishda xatolik yuz berdi.",
            back_keyboard()
        )

    finally:
        active_folders.discard(folder)
        shutil.rmtree(folder, ignore_errors=True)


# =========================================================
# 28. AVTOMATIK FAYL TOZALASH
# =========================================================

async def file_cleaner():
    while True:
        try:
            now = time.time()
            max_age = CLEANUP_AFTER_HOURS * 3600

            for item in Path(DOWNLOAD_DIR).iterdir():
                if not item.is_dir():
                    continue

                if str(item) in active_folders:
                    continue

                try:
                    age = now - item.stat().st_mtime

                    if age > max_age:
                        shutil.rmtree(item, ignore_errors=True)

                except Exception:
                    logging.exception("Papka tozalash xatosi")

        except Exception:
            logging.exception("File cleaner xatosi")

        await asyncio.sleep(600)


# =========================================================
# 29. GLOBAL ERROR HANDLER
# =========================================================

@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logging.error(
        "GLOBAL XATOLIK: %s",
        event.exception,
        exc_info=True
    )

    return True


# =========================================================
# 30. BOTNI ISHGA TUSHIRISH
# =========================================================

async def main():
    if (
        not BOT_TOKEN
        or "BU_YERGA" in BOT_TOKEN
    ):
        raise RuntimeError(
            "BOT_TOKEN qatoriga yangi Telegram bot tokenini yozing."
        )

    init_db()

    if not shutil.which("ffmpeg"):
        logging.warning(
            "FFmpeg topilmadi. MP3 funksiyasi ishlamasligi mumkin."
        )

    asyncio.create_task(file_cleaner())

    await bot.delete_webhook(drop_pending_updates=True)

    logging.info("=" * 45)
    logging.info("MEDIA MASTER PRO %s", BOT_VERSION)
    logging.info("BOT ISHGA TUSHDI")
    logging.info("=" * 45)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print("Bot to'xtatildi.")