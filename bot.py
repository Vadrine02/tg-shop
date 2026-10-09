"""
vadrine — серверна частина Telegram Mini App (aiogram 3 + SQLite + aiohttp).

Що вміє бот:
  • /start відкриває Mini App через нижню клавіатуру (щоб працював sendData);
  • реферальна система: /start ref_<id> дає запросившому +1 спробу колеса;
  • сам визначає виграш колеса (шанси на сервері, клієнт підробити не може);
  • промокод живе 24 години і видаляється з бази: після замовлення АБО через 24 год;
  • через 30 хвилин нагадує про покинутий кошик;
  • приймає замовлення з Mini App (F.web_app_data), перераховує суму сам і
    шле красиве повідомлення адміну та подяку клієнту.

Встановлення:  pip install -U aiogram        (aiohttp встановиться разом з ним)
Запуск:        python bot.py

────────────────────────────────────────────────────────────────────
ЯК ЦЕ ПРАЦЮЄ (коротко)
Mini App, відкритий кнопкою клавіатури, не може написати боту до sendData(),
а sendData() закриває додаток. Тому прокрутку колеса та зміни кошика Mini App
відправляє на HTTP-API цього ж бота (він уже відкритий на Render як Web Service).
Бот підписує посилання на Mini App для кожного користувача (параметри uid та sig),
і по цьому підпису API знає, хто звертається.
Саме замовлення, як і раніше, йде через sendData → F.web_app_data.
────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from urllib.parse import urlencode, urlparse

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo

# ======================================================================
# НАЛАШТУВАННЯ — ВСТАВТЕ СВОЇ ДАНІ
# Найкраще задати їх у Render → Environment (так токен не потрапить на GitHub),
# але можна вписати й прямо сюди замість текстів у лапках.
# ======================================================================

# 1) Токен бота від @BotFather.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8786502270:AAGwuMug8AyV9DHu1pDJFM2851JxE_h2iE4")

# 2) Ваш Telegram ID (число) — на нього приходитимуть замовлення.
#    Дізнатись: бот @userinfobot. Обов'язково натисніть /start у своєму боті!
ADMIN_ID = int(os.getenv("ADMIN_ID", "899013976"))

# 3) ПОСИЛАННЯ НА ВАШ MINI APP НА GITHUB PAGES (HTTPS, без параметрів).
#    Приклад: https://vadrine02.github.io/vadrine/
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://vadrine02.github.io/tg-shop/")

# 4) Публічна адреса цього бота на Render, напр. https://vadrine-bot.onrender.com
#    Render сам задає змінну RENDER_EXTERNAL_URL, тому зазвичай нічого вписувати не треба.
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("https://tg-accessory-bot.onrender.com") or "").rstrip("/")

# 5) Де зберігати базу SQLite.
#    УВАГА: на безкоштовному Render диск тимчасовий — база стирається при кожному
#    деплої/перезапуску. Щоб дані жили вічно, підключіть Render Disk (платно)
#    і вкажіть DB_PATH=/var/data/vadrine.db. Деталі — у повідомленні поруч із кодом.
DB_PATH = os.getenv("DB_PATH", "vadrine.db")

# 6) Реквізити для способу оплати «за реквізитами» — бот надішле їх клієнту після замовлення.
PAYMENT_DETAILS = os.getenv(
    "PAYMENT_DETAILS",
    "IBAN: UA00 0000 0000 0000 0000 0000 00000\nКартка Mono: 0000 0000 0000 0000",
)

# ----------------------------------------------------------------------
# Бізнес-налаштування (мають збігатися з index.html)
# ----------------------------------------------------------------------
CATALOG = {
    "watch":  {"name": "Преміум годинник",  "price": 3290},
    "wallet": {"name": "Шкіряний гаманець", "price": 890},
    "bag":    {"name": "Сумка крос-боді",   "price": 2490},
    "belt":   {"name": "Ремінь",            "price": 690},
}

# Призи колеса. weight = шанс у % (сума 100).
PRIZES = {
    "VAD10":       {"title": "Знижка 10%",                             "weight": 30},
    "FREESHIP600": {"title": "Безкоштовна доставка від 600 ₴",         "weight": 25},
    "3FOR2":       {"title": "Акція «1+1=3»",                           "weight": 20},
    "SECOND20":    {"title": "Знижка 20% на другий товар",             "weight": 20},
    "WESTWOOD":    {"title": "Прикраса Vivienne Westwood у подарунок", "weight": 5},
}

DELIVERY_FEE = 70            # ₴
FREE_SHIP_FROM = 600         # ₴, поріг для FREESHIP600
PROMO_TTL = 24 * 60 * 60     # життя промокоду: 24 години
REMINDER_DELAY = 30 * 60     # нагадування про кошик: 30 хвилин
DEFAULT_SPINS = 1            # скільки спроб у нового користувача
MAX_REFERRALS = 10           # ліміт бонусних спроб за запрошення (захист від накруток)
MAX_QTY = 20

PAYMENTS = {
    "cod":         "Післяплата (накладений платіж Нова Пошта)",
    "iban":        "Оплата за реквізитами (IBAN / картка Mono)",
    "installment": "Покупка частинами (розстрочка банку)",
}

# ======================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vadrine")

router = Router()


# ----------------------------------------------------------------------
# Допоміжні функції
# ----------------------------------------------------------------------
def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₴"


def plural(n: int, one: str, few: str, many: str) -> str:
    a, b = n % 10, n % 100
    if a == 1 and b != 11:
        return one
    if 2 <= a <= 4 and not 12 <= b <= 14:
        return few
    return many


def human_left(seconds: int) -> str:
    """Скільки лишилось: «23 години», «45 хвилин»."""
    seconds = max(0, int(seconds))
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    if hours >= 1:
        return f"{hours} {plural(hours, 'година', 'години', 'годин')}"
    minutes = max(1, minutes)
    return f"{minutes} {plural(minutes, 'хвилина', 'хвилини', 'хвилин')}"


def make_sig(user_id: int) -> str:
    """Підпис користувача для посилання на Mini App (HMAC від токена бота)."""
    return hmac.new(BOT_TOKEN.encode(), f"vadrine:{user_id}".encode(), hashlib.sha256).hexdigest()[:32]


def check_sig(user_id: int, sig: str) -> bool:
    return hmac.compare_digest(make_sig(user_id), str(sig or ""))


def webapp_url_for(user_id: int) -> str:
    """Посилання на Mini App з параметрами: хто користувач і куди слати запити."""
    params = {"uid": user_id, "sig": make_sig(user_id)}
    if PUBLIC_URL:
        params["api"] = PUBLIC_URL
    sep = "&" if "?" in WEBAPP_URL else "?"
    return f"{WEBAPP_URL}{sep}{urlencode(params)}"


def webapp_keyboard(user_id: int, text: str = "🛍 Відкрити vadrine") -> ReplyKeyboardMarkup:
    # Саме KeyboardButton (а не Inline) — інакше sendData() не спрацює!
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text, web_app=WebAppInfo(url=webapp_url_for(user_id)))]],
        resize_keyboard=True,
        is_persistent=True,
    )


# ----------------------------------------------------------------------
# База даних SQLite (стандартна бібліотека, без додаткових залежностей)
# ----------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    first_name  TEXT    NOT NULL DEFAULT '',
    username    TEXT,
    spins_total INTEGER NOT NULL DEFAULT 1,   -- усього спроб (базова + за запрошення)
    spins_used  INTEGER NOT NULL DEFAULT 0,
    referred_by INTEGER,                       -- хто запросив
    referrals   INTEGER NOT NULL DEFAULT 0,    -- скількох запросив цей користувач
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS promos (            -- один активний промокод на користувача
    user_id    INTEGER PRIMARY KEY,
    code       TEXT    NOT NULL,
    issued_at  INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    reminded   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS carts (             -- чи є в користувача товари в кошику
    user_id    INTEGER PRIMARY KEY,
    item_count INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (            -- історія замовлень
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    total      INTEGER NOT NULL,
    promo_code TEXT,
    payload    TEXT    NOT NULL
);
"""


class Database:
    """Синхронні методи; з async-коду викликаються через dbrun() (окремий потік)."""

    def __init__(self, path: str) -> None:
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # --- користувачі -----------------------------------------------------
    def upsert_user(self, user_id: int, first_name: str, username: str | None) -> bool:
        """Створює/оновлює користувача. True — якщо користувач НОВИЙ."""
        with self.lock, self.conn:
            exists = self.conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
            if exists:
                self.conn.execute("UPDATE users SET first_name=?, username=? WHERE user_id=?",
                                  (first_name, username, user_id))
                return False
            self.conn.execute(
                "INSERT INTO users(user_id, first_name, username, spins_total, created_at) VALUES (?,?,?,?,?)",
                (user_id, first_name, username, DEFAULT_SPINS, int(time.time())))
            return True

    def ensure_user(self, user_id: int) -> None:
        """Гарантує, що рядок користувача існує (з базовою кількістю спроб)."""
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO users(user_id, spins_total, created_at) VALUES (?,?,?)",
                (user_id, DEFAULT_SPINS, int(time.time())))

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def apply_referral(self, invitee: int, inviter: int) -> bool:
        """Нараховує запросившому +1 спробу. Лише один раз для нового користувача."""
        if invitee == inviter:
            return False
        with self.lock, self.conn:
            inviter_row = self.conn.execute("SELECT referrals FROM users WHERE user_id=?", (inviter,)).fetchone()
            invitee_row = self.conn.execute("SELECT referred_by FROM users WHERE user_id=?", (invitee,)).fetchone()
            if not inviter_row or not invitee_row or invitee_row["referred_by"] is not None:
                return False
            if inviter_row["referrals"] >= MAX_REFERRALS:
                return False
            self.conn.execute("UPDATE users SET referred_by=? WHERE user_id=?", (inviter, invitee))
            self.conn.execute("UPDATE users SET spins_total=spins_total+1, referrals=referrals+1 WHERE user_id=?",
                              (inviter,))
            return True

    def attempts_left(self, user_id: int) -> int:
        row = self.get_user(user_id)
        return max(0, row["spins_total"] - row["spins_used"]) if row else 0

    # --- промокоди -------------------------------------------------------
    def get_active_promo(self, user_id: int) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM promos WHERE user_id=? AND expires_at>?",
                                    (user_id, int(time.time()))).fetchone()
            return dict(row) if row else None

    def spin(self, user_id: int, code: str) -> tuple[str, dict | None]:
        """Атомарно: перевіряє спроби, списує одну і створює промокод на 24 години."""
        now = int(time.time())
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO users(user_id, spins_total, created_at) VALUES (?,?,?)",
                (user_id, DEFAULT_SPINS, now))
            promo = self.conn.execute("SELECT * FROM promos WHERE user_id=? AND expires_at>?",
                                      (user_id, now)).fetchone()
            if promo:
                return "active_promo", dict(promo)
            user = self.conn.execute("SELECT spins_total, spins_used FROM users WHERE user_id=?",
                                     (user_id,)).fetchone()
            if user["spins_total"] - user["spins_used"] < 1:
                return "no_attempts", None
            self.conn.execute("UPDATE users SET spins_used=spins_used+1 WHERE user_id=?", (user_id,))
            self.conn.execute(
                "INSERT OR REPLACE INTO promos(user_id, code, issued_at, expires_at, reminded) VALUES (?,?,?,?,0)",
                (user_id, code, now, now + PROMO_TTL))
            return "ok", {"user_id": user_id, "code": code, "issued_at": now, "expires_at": now + PROMO_TTL}

    def delete_promo(self, user_id: int, expires_at: int | None = None) -> bool:
        """Видаляє промокод. Якщо вказано expires_at — лише той самий (щоб старий таймер
        не видалив новий промокод)."""
        with self.lock, self.conn:
            if expires_at is None:
                cur = self.conn.execute("DELETE FROM promos WHERE user_id=?", (user_id,))
            else:
                cur = self.conn.execute("DELETE FROM promos WHERE user_id=? AND expires_at=?",
                                        (user_id, expires_at))
            return cur.rowcount > 0

    def mark_reminded(self, user_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE promos SET reminded=1 WHERE user_id=?", (user_id,))

    def list_promos(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM promos")]

    # --- кошик -----------------------------------------------------------
    def set_cart(self, user_id: int, count: int) -> None:
        with self.lock, self.conn:
            if count > 0:
                self.conn.execute("INSERT OR REPLACE INTO carts(user_id, item_count, updated_at) VALUES (?,?,?)",
                                  (user_id, count, int(time.time())))
            else:
                self.conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))

    def get_cart(self, user_id: int) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM carts WHERE user_id=?", (user_id,)).fetchone()
            return dict(row) if row else None

    def list_carts(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM carts")]

    # --- замовлення ------------------------------------------------------
    def consume_order(self, user_id: int, payload: str, total: int, promo_code: str | None) -> int:
        """Зберігає замовлення і В ТІЙ САМІЙ ТРАНЗАКЦІЇ видаляє промокод та кошик."""
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO orders(user_id, created_at, total, promo_code, payload) VALUES (?,?,?,?,?)",
                (user_id, int(time.time()), total, promo_code, payload))
            self.conn.execute("DELETE FROM promos WHERE user_id=?", (user_id,))
            self.conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
            return cur.lastrowid


async def dbrun(fn, *args):
    """Виконує синхронний метод БД в окремому потоці, щоб не блокувати бота."""
    return await asyncio.to_thread(fn, *args)


# ----------------------------------------------------------------------
# Таймери (asyncio.create_task): згорання промокоду та нагадування про кошик
# ----------------------------------------------------------------------
class Scheduler:
    def __init__(self, bot: Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self.expiry: dict[int, asyncio.Task] = {}
        self.reminders: dict[int, asyncio.Task] = {}

    # --- 24 години: видалити промокод ------------------------------------
    def schedule_expiry(self, user_id: int, expires_at: int) -> None:
        old = self.expiry.pop(user_id, None)
        if old:
            old.cancel()
        self.expiry[user_id] = asyncio.create_task(self._expiry_worker(user_id, expires_at))

    async def _expiry_worker(self, user_id: int, expires_at: int) -> None:
        try:
            await asyncio.sleep(max(0, expires_at - time.time()))
            if await dbrun(self.db.delete_promo, user_id, expires_at):
                log.info("Промокод користувача %s згорів (24 год) і видалений з бази", user_id)
            self.cancel_reminder(user_id)
            await dbrun(self.db.set_cart, user_id, 0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Помилка таймера згорання для %s", user_id)
        finally:
            if self.expiry.get(user_id) is asyncio.current_task():
                self.expiry.pop(user_id, None)

    # --- 30 хвилин: нагадати про кошик -----------------------------------
    def schedule_reminder(self, user_id: int, delay: float = REMINDER_DELAY) -> None:
        self.cancel_reminder(user_id)
        self.reminders[user_id] = asyncio.create_task(self._reminder_worker(user_id, delay))

    def cancel_reminder(self, user_id: int) -> None:
        task = self.reminders.pop(user_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    def cancel_all(self, user_id: int) -> None:
        self.cancel_reminder(user_id)
        task = self.expiry.pop(user_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    async def _reminder_worker(self, user_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(max(0, delay))
            promo = await dbrun(self.db.get_active_promo, user_id)
            cart = await dbrun(self.db.get_cart, user_id)
            # нагадуємо лише якщо: промокод ще діє, товари в кошику є і ми ще не нагадували
            if not promo or promo["reminded"] or not cart or cart["item_count"] < 1:
                return
            user = await dbrun(self.db.get_user, user_id)
            # Ім'я береться як є (у Telegram воно в називному відмінку; автоматично
            # відмінювати «Владислав» → «Владиславе» не вийде).
            name = esc(user["first_name"]) if user and user["first_name"] else "Друже"
            left = human_left(promo["expires_at"] - time.time())
            text = (
                f"{name}, ми помітили, що ви залишили товари у кошику. 🛍\n"
                f"Нагадуємо, що ваш промокод <b>{esc(promo['code'])}</b> діє ще <b>{left}</b>!\n\n"
                "Завершити замовлення можна кнопкою внизу 👇"
            )
            # Кнопка — у reply-клавіатурі, щоб sendData() спрацював після відкриття
            await self.bot.send_message(user_id, text,
                                        reply_markup=webapp_keyboard(user_id, "🛍 Завершити замовлення"))
            await dbrun(self.db.mark_reminded, user_id)
            log.info("Надіслано нагадування про кошик користувачу %s", user_id)
        except asyncio.CancelledError:
            raise
        except TelegramForbiddenError:
            log.info("Користувач %s заблокував бота — нагадування пропущено", user_id)
        except TelegramAPIError:
            log.exception("Не вдалося надіслати нагадування користувачу %s", user_id)
        except Exception:
            log.exception("Помилка таймера нагадування для %s", user_id)
        finally:
            if self.reminders.get(user_id) is asyncio.current_task():
                self.reminders.pop(user_id, None)

    # --- після перезапуску бота відновлюємо таймери з бази ---------------
    async def restore(self) -> None:
        now = time.time()
        promos = await dbrun(self.db.list_promos)
        active = {}
        for p in promos:
            if p["expires_at"] <= now:
                await dbrun(self.db.delete_promo, p["user_id"], p["expires_at"])
                log.info("Прострочений промокод %s видалено при старті", p["user_id"])
            else:
                active[p["user_id"]] = p
                self.schedule_expiry(p["user_id"], p["expires_at"])
        for cart in await dbrun(self.db.list_carts):
            p = active.get(cart["user_id"])
            if p and not p["reminded"]:
                self.schedule_reminder(cart["user_id"], cart["updated_at"] + REMINDER_DELAY - now)
        log.info("Відновлено таймерів: промокодів %s", len(active))


# ----------------------------------------------------------------------
# Розрахунок замовлення (ті самі правила, що у фронтенді)
# ----------------------------------------------------------------------
def calculate(items: list[dict], promo: str | None) -> dict:
    units = sorted((it["price"] for it in items for _ in range(it["qty"])), reverse=True)
    subtotal = sum(units)
    discount = 0
    if promo == "VAD10":
        discount = (subtotal * 10 + 50) // 100              # −10%, округлення вгору від .5
    elif promo == "3FOR2":
        discount = sum(units[2::3])                         # кожна 3-тя (найдешевша в трійці) безкоштовна
    elif promo == "SECOND20" and len(units) >= 2:
        discount = (units[1] * 20 + 50) // 100              # −20% на другу за вартістю одиницю
    free_ship = promo == "FREESHIP600" and subtotal >= FREE_SHIP_FROM
    delivery = 0 if free_ship else DELIVERY_FEE
    return {
        "subtotal": subtotal, "discount": discount, "delivery": delivery,
        "free_ship": free_ship, "total": subtotal - discount + delivery, "units": len(units),
    }


def clean_text(value: object, limit: int = 100) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_order(raw: str, promo: str | None) -> dict:
    """Перевіряє JSON з Mini App. Ціни та промокод беруться ЗІ СВОГО боку (каталог і база),
    значення з JSON використовуються лише як довідкові."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Некоректний JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Некоректна структура даних")

    customer = data.get("customer") or {}
    name = clean_text(customer.get("name"))
    phone = "".join(ch for ch in str(customer.get("phone", "")) if ch.isdigit() or ch == "+")[:16]
    city = clean_text(customer.get("city"))
    branch = "".join(ch for ch in str(customer.get("branch", "")) if ch.isdigit())[:5]
    if not (name and city and branch and sum(c.isdigit() for c in phone) >= 10):
        raise ValueError("Не всі дані клієнта заповнені")

    items = []
    for entry in data.get("items") or []:
        if not isinstance(entry, dict):
            continue
        product = CATALOG.get(str(entry.get("id")))
        try:
            qty = int(entry.get("q", 0))
        except (TypeError, ValueError):
            continue
        if product and qty >= 1:
            qty = min(qty, MAX_QTY)
            items.append({"name": product["name"], "price": product["price"], "qty": qty,
                          "sum": product["price"] * qty})
    if not items:
        raise ValueError("Порожній кошик")

    payment = data.get("payment") if data.get("payment") in PAYMENTS else "cod"
    calc = calculate(items, promo)
    client_promo = str(data.get("promo") or "").upper() or None
    return {
        "customer": {"name": name, "phone": phone, "city": city, "branch": branch},
        "items": items, "payment": payment, "promo": promo, "client_promo": client_promo,
        "client_total": data.get("total"), "gift_choice": clean_text(data.get("gift_choice"), 80),
        **calc,
    }


def promo_notes(order: dict) -> list[str]:
    promo = order["promo"]
    notes = []
    if promo == "3FOR2":
        notes.append(f"✨ Акція 1+1=3: безкоштовним зараховано найдешевший товар у кожній трійці (−{money(order['discount'])}).")
        if order["units"] < 3:
            notes.append("ℹ️ Акція не спрацювала: у замовленні менше трьох товарів.")
    elif promo == "SECOND20" and order["units"] < 2:
        notes.append("ℹ️ SECOND20 не спрацював: у замовленні лише один товар.")
    elif promo == "FREESHIP600" and not order["free_ship"]:
        notes.append(f"ℹ️ FREESHIP600 не спрацював: сума товарів менша за {money(FREE_SHIP_FROM)}.")
    elif promo == "WESTWOOD":
        wish = f" Побажання клієнта: <i>{esc(order['gift_choice'])}</i>" if order["gift_choice"] else " Побажань клієнт не вказав."
        notes.append(f"💎 Покладіть у посилку прикрасу Vivienne Westwood.{wish}")
    if order["client_promo"] != promo:
        notes.append(f"⚠️ У Mini App клієнт бачив промокод <b>{esc(order['client_promo'] or 'немає')}</b>, "
                     f"а в базі зараз: <b>{esc(promo or 'немає')}</b> (міг згоріти). Сума розрахована за базою.")
    if isinstance(order["client_total"], (int, float)) and order["client_total"] != order["total"]:
        notes.append(f"⚠️ Сума в Mini App ({money(int(order['client_total']))}) відрізняється від розрахованої.")
    return notes


def admin_text(order: dict, order_no: str, message: Message) -> str:
    user = message.from_user
    c = order["customer"]
    username = f"@{esc(user.username)}" if user.username else "—"
    lines = [
        f"🛍 <b>Нове замовлення {order_no}</b>",
        "",
        "👤 <b>Клієнт</b>",
        f"ПІБ: {esc(c['name'])}",
        f"Телефон: <code>{esc(c['phone'])}</code>",
        f"Нова Пошта: {esc(c['city'])}, відділення №{esc(c['branch'])}",
        f'Telegram: <a href="tg://user?id={user.id}">{esc(user.full_name)}</a> ({username}), ID <code>{user.id}</code>',
        "",
        f"💳 <b>Оплата:</b> {PAYMENTS[order['payment']]}",
        "",
        "📦 <b>Товари</b>",
    ]
    for i, it in enumerate(order["items"], 1):
        lines.append(f"{i}. {esc(it['name'])} × {it['qty']} — {money(it['sum'])}")
    lines += ["", f"Товари: {money(order['subtotal'])}"]
    if order["discount"]:
        lines.append(f"Знижка: −{money(order['discount'])}")
    lines.append(f"Доставка: {money(order['delivery']) if order['delivery'] else 'безкоштовно'}")
    lines.append(f"<b>Разом до сплати: {money(order['total'])}</b>")
    if order["promo"]:
        lines += ["", f"🎟 Промокод клієнта: <b>{order['promo']}</b> ({esc(PRIZES[order['promo']]['title'])})"]
    else:
        lines += ["", "🎟 Промокод: немає"]
    notes = promo_notes(order)
    if notes:
        lines += [""] + notes
    return "\n".join(lines)


def client_text(order: dict, order_no: str) -> str:
    c = order["customer"]
    text = (
        f"✅ <b>Дякуємо за замовлення {order_no}!</b>\n\n"
        f"Сума до сплати: <b>{money(order['total'])}</b>\n"
        f"Доставка: {esc(c['city'])}, відділення Нової Пошти №{esc(c['branch'])}\n"
        f"Оплата: {PAYMENTS[order['payment']]}\n"
    )
    if order["payment"] == "iban":
        text += f"\nРеквізити для оплати:\n<code>{esc(PAYMENT_DETAILS)}</code>\nУ призначенні вкажіть номер замовлення {order_no}.\n"
    elif order["payment"] == "installment":
        text += "\nМенеджер зв'яжеться з вами, щоб оформити розстрочку.\n"
    else:
        text += "\nОплата при отриманні у відділенні Нової Пошти.\n"
    if order["promo"] == "WESTWOOD":
        text += "\n💎 Подарункова прикраса Vivienne Westwood вже чекає вас у посилці!\n"
    return text + "\nМенеджер зв'яжеться з вами найближчим часом. 💜"


# ----------------------------------------------------------------------
# Реферали
# ----------------------------------------------------------------------
def parse_ref(payload: str | None) -> int | None:
    """Приймає 'ref_123456', 'ref=123456' або просто '123456'."""
    if not payload:
        return None
    m = re.fullmatch(r"(?:ref[_=-]?)?(\d{4,15})", payload.strip())
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------
# Обробники повідомлень
# ----------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    user = message.from_user
    is_new = await dbrun(db.upsert_user, user.id, user.first_name or "", user.username)

    # Реферальний хвіст: t.me/<бот>?start=ref_123456
    ref = parse_ref(command.args)
    if is_new and ref:
        if await dbrun(db.apply_referral, user.id, ref):
            log.info("Реферал: %s запросив %s", ref, user.id)
            try:
                await bot.send_message(
                    ref, f"🎉 Ваш друг <b>{esc(user.first_name)}</b> приєднався до vadrine!\n"
                         "Вам нараховано <b>+1 спробу</b> колеса фортуни.")
            except TelegramAPIError:
                log.warning("Не вдалося повідомити запросившого %s", ref)

    left = await dbrun(db.attempts_left, user.id)
    await message.answer(
        f"Привіт, <b>{esc(user.first_name)}</b>! 👋\n\n"
        "Ласкаво просимо до <b>vadrine</b> — аксесуари з характером.\n"
        f"Спроб на колесо фортуни: <b>{left}</b> 🎁\n\n"
        "Натисніть кнопку внизу, щоб відкрити магазин.\n"
        "Запросити друга і отримати ще спробу: /invite",
        reply_markup=webapp_keyboard(user.id),
    )


@router.message(Command("invite"))
async def cmd_invite(message: Message, bot: Bot) -> None:
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    await message.answer(
        "🎁 <b>Запросіть друга</b> — коли він відкриє бота вперше, ви отримаєте +1 спробу колеса.\n\n"
        f"Ваше посилання:\n{link}")


@router.message(Command("backup"), F.from_user.id == ADMIN_ID)
async def cmd_backup(message: Message, db: Database) -> None:
    """Лише для адміна: надсилає файл бази (страховка на безкоштовному Render)."""
    await message.answer_document(FSInputFile(db.path), caption="Резервна копія бази vadrine")


@router.message(F.web_app_data)
async def on_web_app_data(message: Message, bot: Bot, db: Database, scheduler: Scheduler) -> None:
    user = message.from_user
    await dbrun(db.upsert_user, user.id, user.first_name or "", user.username)

    # Промокод беремо з БАЗИ, а не з JSON — клієнт не може підробити знижку
    promo_row = await dbrun(db.get_active_promo, user.id)
    promo = promo_row["code"] if promo_row else None

    try:
        order = parse_order(message.web_app_data.data, promo)
    except ValueError as exc:
        log.warning("Невалідне замовлення від %s: %s", user.id, exc)
        await message.answer("😕 Не вдалося обробити замовлення. Відкрийте магазин і спробуйте ще раз.")
        return

    # Зберігаємо замовлення і ОДРАЗУ видаляємо промокод та кошик (одна транзакція)
    order_id = await dbrun(db.consume_order, user.id, message.web_app_data.data, order["total"], promo)
    scheduler.cancel_all(user.id)  # таймери згорання і нагадування більше не потрібні
    order_no = f"VD-{time.strftime('%d%m')}-{order_id}"
    log.info("Замовлення %s від %s на %s ₴, промокод %s видалено", order_no, user.id, order["total"], promo)

    try:
        await bot.send_message(ADMIN_ID, admin_text(order, order_no, message))
    except TelegramAPIError:
        log.exception("Не вдалося надіслати замовлення %s адміну (ADMIN_ID=%s)", order_no, ADMIN_ID)

    await message.answer(client_text(order, order_no), reply_markup=webapp_keyboard(user.id))


# ----------------------------------------------------------------------
# HTTP-API для Mini App (працює в одному процесі з ботом)
# ----------------------------------------------------------------------
def webapp_origin() -> str:
    p = urlparse(WEBAPP_URL)
    return f"{p.scheme}://{p.netloc}"


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Дозволяє запити лише з вашого сайту на GitHub Pages."""
    if request.method == "OPTIONS":
        response: web.StreamResponse = web.Response(status=204)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
    origin = request.headers.get("Origin")
    if origin and origin == webapp_origin():
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Vary"] = "Origin"
    return response


def auth_user(uid_raw, sig) -> int:
    try:
        uid = int(uid_raw)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="bad uid")
    if not check_sig(uid, sig):
        raise web.HTTPForbidden(text="bad signature")
    return uid


async def read_json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="bad json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="bad json")
    return data


async def state_payload(db: Database, uid: int) -> dict:
    attempts = await dbrun(db.attempts_left, uid)
    promo = await dbrun(db.get_active_promo, uid)
    return {
        "ok": True,
        "attempts": attempts,
        "prize": {"code": promo["code"], "remaining": promo["expires_at"] - int(time.time())} if promo else None,
    }


async def api_health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def api_state(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    uid = auth_user(request.query.get("uid"), request.query.get("sig"))
    await dbrun(db.ensure_user, uid)
    return web.json_response(await state_payload(db, uid))


async def api_spin(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    scheduler: Scheduler = request.app["scheduler"]
    data = await read_json(request)
    uid = auth_user(data.get("uid"), data.get("sig"))

    # Приз обирає СЕРВЕР за вагами — клієнт на результат вплинути не може
    code = random.choices(list(PRIZES), weights=[p["weight"] for p in PRIZES.values()])[0]
    status, promo = await dbrun(db.spin, uid, code)

    if status == "ok":
        scheduler.schedule_expiry(uid, promo["expires_at"])  # через 24 год промокод буде видалено
        log.info("Користувач %s виграв %s", uid, code)
        payload = await state_payload(db, uid)
        payload["code"] = promo["code"]
        payload["remaining"] = promo["expires_at"] - int(time.time())
        return web.json_response(payload)

    payload = await state_payload(db, uid)
    payload.update(ok=False, error=status)  # 'no_attempts' або 'active_promo'
    return web.json_response(payload)


async def api_cart(request: web.Request) -> web.Response:
    """Mini App повідомляє, скільки товарів у кошику — від цього залежить нагадування."""
    db: Database = request.app["db"]
    scheduler: Scheduler = request.app["scheduler"]
    data = await read_json(request)
    uid = auth_user(data.get("uid"), data.get("sig"))
    try:
        count = max(0, min(int(data.get("count", 0)), 99))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="bad count")

    await dbrun(db.set_cart, uid, count)
    promo = await dbrun(db.get_active_promo, uid)
    if count > 0 and promo and not promo["reminded"]:
        scheduler.schedule_reminder(uid)   # (пере)запускаємо відлік 30 хвилин
    else:
        scheduler.cancel_reminder(uid)     # кошик порожній / нагадування вже було / промокоду немає
    return web.json_response({"ok": True})


async def start_http_server(db: Database, scheduler: Scheduler) -> web.AppRunner:
    app = web.Application(middlewares=[cors_middleware])
    app["db"], app["scheduler"] = db, scheduler
    app.router.add_get("/", api_health)          # для Render і UptimeRobot
    app.router.add_get("/health", api_health)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/spin", api_spin)
    app.router.add_post("/api/cart", api_cart)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))        # Render сам задає PORT
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("HTTP-сервер слухає порт %s (API для Mini App: %s)", port, PUBLIC_URL or "PUBLIC_URL не задано!")
    return runner


# ----------------------------------------------------------------------
async def main() -> None:
    if "ВСТАВТЕ" in BOT_TOKEN:
        raise SystemExit("Вкажіть BOT_TOKEN (змінна середовища або рядок у bot.py)")
    if ADMIN_ID == 123456789:
        log.warning("ADMIN_ID не змінено — замовлення не дійдуть до вас!")
    if "ВАШ_НІК" in WEBAPP_URL:
        log.warning("WEBAPP_URL не змінено — кнопка відкриватиме неіснуючу сторінку!")
    if not PUBLIC_URL:
        log.warning("PUBLIC_URL не задано — Mini App не зможе звʼязатися з сервером (працюватиме без бази)")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    db = Database(DB_PATH)
    scheduler = Scheduler(bot, db)

    dp = Dispatcher(db=db, scheduler=scheduler)   # ці об'єкти потрапляють в обробники за іменем
    dp.include_router(router)

    await scheduler.restore()
    runner = await start_http_server(db, scheduler)
    try:
        # pending-оновлення НЕ скидаємо: замовлення, надіслані поки бот спав, не втратяться
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("Бот запущено")
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
