"""
vadrine — серверна частина Telegram Mini App (aiogram 3 + SQLite + aiohttp).

Що вміє бот:
  • /start відкриває Mini App через нижню клавіатуру (щоб працював sendData);
  • реферальна система: /start ref_<id> дає запросившому +1 спробу колеса;
  • адмін видає прокрути: /give <id або @username> [кількість], /giveall <кількість>, /spins;
  • крутити можна, поки є спроби; новий виграш замінює попередній промокод;
  • сам визначає виграш колеса (шанси на сервері, клієнт підробити не може);
  • промокод живе 24 години і видаляється з бази: після замовлення АБО через 24 год;
  • через 30 хвилин нагадує про покинутий кошик;
  • зберігає дані в хмарній базі Turso (або в локальному SQLite, якщо Turso не задано);
  • відстежує посилки Нової Пошти за ТТН: /setttn (адмін) + кнопка «Відстежити посилку» в Mini App;
  • показує історію замовлень: /myorders у чаті та вкладка «Мої замовлення» в Mini App;
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
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import aiohttp                # встановлюється разом з aiogram
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
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВТЕ_ТОКЕН_БОТА")

# 2) Ваш Telegram ID (число) — на нього приходитимуть замовлення.
#    Дізнатись: бот @userinfobot. Обов'язково натисніть /start у своєму боті!
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))

# 3) ПОСИЛАННЯ НА ВАШ MINI APP НА GITHUB PAGES (HTTPS, без параметрів).
#    Приклад: https://vadrine02.github.io/vadrine/
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://vadrine02.github.io/tg-shop/")

# 4) Публічна адреса цього бота на Render, напр. https://vadrine-bot.onrender.com
#    Render сам задає змінну RENDER_EXTERNAL_URL, тому зазвичай нічого вписувати не треба.
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

# 5) БАЗА ДАНИХ.
#    Варіант А (рекомендовано для Render Free) — хмарна база Turso: дані НЕ зникають
#    при перезапусках. Вкажіть дві змінні (Render → Environment):
#       TURSO_DATABASE_URL = libsql://назва-бази-ваш_нік.turso.io
#       TURSO_AUTH_TOKEN   = токен (turso db tokens create назва-бази)
#    Варіант Б — локальний файл SQLite (якщо Turso не задано). На безкоштовному Render
#    диск тимчасовий і файл стирається при кожному деплої/перезапуску.
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "vadrine.db")   # лише для локального режиму

# 6) Реквізити для способу оплати «за реквізитами» — бот надішле їх клієнту після замовлення.
PAYMENT_DETAILS = os.getenv(
    "PAYMENT_DETAILS",
    "IBAN: UA00 0000 0000 0000 0000 0000 00000\nКартка Mono: 0000 0000 0000 0000",
)

# 7) Ключ API Нової Пошти — НЕОБОВ'ЯЗКОВО. Публічне відстеження за ТТН зазвичай працює й без ключа.
#    Якщо Нова Пошта почне повертати помилку — отримайте безкоштовний ключ у особистому кабінеті
#    business.novaposhta.ua (Налаштування → Безпека → Ключі API) і вкажіть його тут або в Render → Environment.
NP_API_KEY = os.getenv("NP_API_KEY", "").strip()

# ----------------------------------------------------------------------
# Бізнес-налаштування (мають збігатися з index.html)
# ----------------------------------------------------------------------
CATALOG = {

    "bracelet1":  {"name": "Vivienne Westwood Mayfair Bas Relief Bracelet",  "price": 549},
    "necklace1": {"name": "Vivienne Westwood Mayfair Large Orb Pendant Necklace", "price": 399},
    "necklace2":    {"name": "Vivienne Westwood Mini Bas Relief Pendant Necklace чорний",   "price": 399},
    "necklace3":   {"name": "Vivienne Westwood Mini Bas Relief Pendant Necklace рожевий",            "price": 399},
    "necklace4":   {"name": "Vivienne Westwood Mini Bas Relief Pendant Necklace фіолетовий",            "price": 399},
    "necklace5":   {"name": "Vivienne Westwood Mini Bas Relief Pendant Necklace прозорий",            "price": 399},
    "necklace6":   {"name": "Vivienne Westwood Mini Bas Relief Pendant Necklace червоний",            "price": 399},
    "necklace7":   {"name": "Vivienne Westwood Mini Bas Relief Pendant Necklace блакитний",            "price": 399},
    "necklace8":   {"name": "Кулон Vivienne Westwood ",            "price": 299},
    "necklace9":   {"name": "Кулон скріпка Vivienne Westwood",            "price": 299},
    "bracelet2":  {"name": "Vivienne Westwood Lucrece Safety Pin Orb Bracelet",  "price": 449},
    "necklace10":   {"name": "Vivienne Westwood Bat Wing Orb Necklace ",            "price": 399},
    "earrings1":   {"name": "Сережки Vivienne Westwood",            "price": 299},
    "necklace11":   {"name": "Vivienne Westwood Ornamental Small Orb Pendant Necklace чорний",            "price": 449},
    "necklace12":   {"name": "Vivienne Westwood Ornamental Small Orb Pendant Necklace фіолетовий",            "price": 449},
    "necklace13":   {"name": "Vivienne Westwood New Small Orb Pendant Necklace червоний",            "price": 499},
    "necklace14":   {"name": "Vivienne Westwood New Small Orb Pendant Necklace синій",            "price": 499},
    "necklace15":   {"name": "Vivienne Westwood New Small Orb Pendant Necklace чорний",            "price": 499},
    "wallet1":   {"name": "Vivienne Westwood Vintage Water Orb Tri-Fold Wallet",            "price": 1299},
    "necklace16":   {"name": "Pandora Sparkling Heart Collier Necklace",            "price": 499},
    "clutch1":   {"name": "Zadig & Voltaire Rock Clutch White",            "price": 1399},
    "clutch2":   {"name": "Zadig & Voltaire Rock Clutch Black",            "price": 1399},
    "clutch3":   {"name": "Zadig & Voltaire Rock Quilted Leather Clutch White",            "price": 1399},
    "clutch4":   {"name": "Zadig & Voltaire Rock Quilted Leather Clutch Black",            "price": 1399},
    "clutch5":   {"name": "Zadig & Voltaire Rock Studs Clutch",            "price": 1699},

}

# Призи колеса. weight = шанс у % (сума 100).
PRIZES = {
    "VAD10":       {"title": "Знижка 10%",                             "weight": 30},
    "FREESHIP600": {"title": "Безкоштовна доставка від 600 ₴",         "weight": 25},
    "3FOR2":       {"title": "Акція «1+1=3»",                           "weight": 20},
    "SECOND20":    {"title": "Знижка 20% на другий товар",             "weight": 20},
    "WESTWOOD":    {"title": "Прикраса Vivienne Westwood у подарунок", "weight": 5},
}

# Вартість доставки в суму замовлення НЕ входить: Нова Пошта бере її за своїм тарифом.
# Приз FREESHIP600 означає, що відправлення (від суми нижче) оплачуєте ви.
FREE_SHIP_FROM = 600         # ₴, поріг для FREESHIP600
PROMO_TTL = 24 * 60 * 60     # життя промокоду: 24 години
REMINDER_DELAY = 30 * 60     # нагадування про кошик: 30 хвилин
DEFAULT_SPINS = 1            # скільки спроб у НОВОГО користувача (хочете більше — поставте 3, 5…)
MAX_REFERRALS = 10           # ліміт бонусних спроб за запрошення (захист від накруток)
MAX_QTY = 20

PAYMENTS = {
    "cod":  "Післяплата (передплата 100 ₴, решта при отриманні)",
    "iban": "Повна оплата на рахунок IBAN",
}
PAYMENT_SHORT = {"cod": "Післяплата", "iban": "Оплата на IBAN"}   # для списків замовлень
PREPAYMENT = 100             # ₴, передплата при післяплаті

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


try:
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:
    try:
        KYIV = ZoneInfo("Europe/Kiev")
    except Exception:                      # немає бази часових поясів
        KYIV = timezone(timedelta(hours=3))


def fmt_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, KYIV).strftime("%d.%m.%Y %H:%M")


def order_number(order_id: int, ts: int) -> str:
    return f"VD-{datetime.fromtimestamp(ts, KYIV).strftime('%d%m')}-{order_id}"


def make_sig(user_id: int) -> str:
    """Підпис користувача для посилання на Mini App (HMAC від токена бота)."""
    return hmac.new(BOT_TOKEN.encode(), f"vadrine:{user_id}".encode(), hashlib.sha256).hexdigest()[:32]


def check_sig(user_id: int, sig: str) -> bool:
    return hmac.compare_digest(make_sig(user_id), str(sig or ""))


def webapp_url_for(user_id: int, **extra: str) -> str:
    """Посилання на Mini App з параметрами: хто користувач і куди слати запити.
    extra — додаткові параметри, напр. open="orders" (одразу відкрити «Мої замовлення»)."""
    params = {"uid": user_id, "sig": make_sig(user_id)}
    if PUBLIC_URL:
        params["api"] = PUBLIC_URL
    params.update(extra)
    sep = "&" if "?" in WEBAPP_URL else "?"
    return f"{WEBAPP_URL}{sep}{urlencode(params)}"


def webapp_keyboard(user_id: int, text: str = "🛍 Відкрити vadrine", **extra: str) -> ReplyKeyboardMarkup:
    # Саме KeyboardButton (а не Inline) — інакше sendData() не спрацює!
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text, web_app=WebAppInfo(url=webapp_url_for(user_id, **extra)))]],
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
    spins_total INTEGER NOT NULL DEFAULT 1,
    spins_used  INTEGER NOT NULL DEFAULT 0,
    referred_by INTEGER,
    referrals   INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS promos (
    user_id    INTEGER PRIMARY KEY,
    code       TEXT    NOT NULL,
    issued_at  INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    reminded   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS carts (
    user_id    INTEGER PRIMARY KEY,
    item_count INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    total      INTEGER NOT NULL,
    promo_code TEXT,
    payload    TEXT    NOT NULL,
    details    TEXT,
    ttn        TEXT
)
"""

# Колонки вказуємо явно: libsql (Turso) повертає прості кортежі, а не sqlite3.Row
USER_COLS = ("user_id", "first_name", "username", "spins_total", "spins_used", "referred_by", "referrals", "created_at")
PROMO_COLS = ("user_id", "code", "issued_at", "expires_at", "reminded")
CART_COLS = ("user_id", "item_count", "updated_at")
ORDER_COLS = ("id", "user_id", "created_at", "total", "promo_code", "payload", "details", "ttn")


def _sel(cols: tuple) -> str:
    return ", ".join(cols)


class Database:
    """Працює і з локальним SQLite, і з хмарною Turso (libsql).
    Усі операції виконуються в ОДНОМУ окремому потоці (self.executor) — з async-коду
    їх викликають через dbrun(); libsql не любить, коли з'єднанням користуються з різних потоків."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.remote = bool(TURSO_URL and TURSO_TOKEN)
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")
        self.executor.submit(self._connect).result()   # з'єднання створюється саме в потоці БД

    def _connect(self) -> None:
        if self.remote:
            import libsql_experimental as libsql        # імпорт лише коли Turso задано
            self.conn = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)
            # PRAGMA journal_mode=WAL для Turso/LibSQL не потрібен — пропускаємо
        else:
            folder = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(folder, exist_ok=True)
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                self.conn.execute(statement)
        self.conn.commit()
        for column in ("details", "ttn"):               # міграція старих баз: додаємо нові колонки
            try:
                self.conn.execute(f"SELECT {column} FROM orders LIMIT 1").fetchall()
            except Exception:
                self.conn.execute(f"ALTER TABLE orders ADD COLUMN {column} TEXT")
                self.conn.commit()

    def close(self) -> None:
        def _close() -> None:
            with self.lock:
                self.conn.close()
        try:
            self.executor.submit(_close).result(timeout=10)
        finally:
            self.executor.shutdown(wait=False)

    # --- службові ---------------------------------------------------------
    @contextmanager
    def tx(self):
        """Транзакція: commit при успіху, rollback при помилці."""
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise

    def _one(self, sql: str, params: tuple, cols: tuple) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(zip(cols, row)) if row else None

    def _all(self, sql: str, params: tuple, cols: tuple) -> list[dict]:
        return [dict(zip(cols, row)) for row in self.conn.execute(sql, params).fetchall()]

    # --- користувачі -----------------------------------------------------
    def upsert_user(self, user_id: int, first_name: str, username: str | None) -> bool:
        """Створює/оновлює користувача. True — якщо користувач НОВИЙ."""
        with self.tx():
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
        with self.tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO users(user_id, spins_total, created_at) VALUES (?,?,?)",
                (user_id, DEFAULT_SPINS, int(time.time())))

    def find_user(self, ref: str) -> dict | None:
        """Шукає користувача за ID (число) або @username."""
        ref = ref.strip()
        with self.lock:
            if ref.isdigit():
                return self._one(f"SELECT {_sel(USER_COLS)} FROM users WHERE user_id=?", (int(ref),), USER_COLS)
            return self._one(f"SELECT {_sel(USER_COLS)} FROM users WHERE lower(username)=lower(?)",
                             (ref.lstrip("@"),), USER_COLS)

    def get_user(self, user_id: int) -> dict | None:
        with self.lock:
            return self._one(f"SELECT {_sel(USER_COLS)} FROM users WHERE user_id=?", (user_id,), USER_COLS)

    def add_spins(self, user_id: int, delta: int) -> int:
        """Додає (або віднімає) спроби. Нижче вже використаних не опуститься. Повертає, скільки лишилось."""
        with self.tx():
            self.conn.execute("UPDATE users SET spins_total = MAX(spins_used, spins_total + ?) WHERE user_id=?",
                              (delta, user_id))
        return self.attempts_left(user_id)

    def add_spins_all(self, delta: int) -> int:
        """Додає спроби ВСІМ користувачам (акція). Повертає, скількох користувачів оновлено."""
        with self.tx():
            count = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            self.conn.execute("UPDATE users SET spins_total = MAX(spins_used, spins_total + ?)", (delta,))
            return count

    def apply_referral(self, invitee: int, inviter: int) -> bool:
        """Нараховує запросившому +1 спробу. Лише один раз для нового користувача."""
        if invitee == inviter:
            return False
        with self.tx():
            inviter_row = self._one("SELECT referrals FROM users WHERE user_id=?", (inviter,), ("referrals",))
            invitee_row = self._one("SELECT referred_by FROM users WHERE user_id=?", (invitee,), ("referred_by",))
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
            return self._one(f"SELECT {_sel(PROMO_COLS)} FROM promos WHERE user_id=? AND expires_at>?",
                             (user_id, int(time.time())), PROMO_COLS)

    def spin(self, user_id: int, code: str) -> tuple[str, dict | None]:
        """Атомарно: перевіряє спроби, списує одну і створює промокод на 24 години.
        Якщо в користувача вже є активний промокод — новий виграш ЗАМІНЮЄ його."""
        now = int(time.time())
        with self.tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO users(user_id, spins_total, created_at) VALUES (?,?,?)",
                (user_id, DEFAULT_SPINS, now))
            user = self._one("SELECT spins_total, spins_used FROM users WHERE user_id=?", (user_id,),
                             ("spins_total", "spins_used"))
            if user["spins_total"] - user["spins_used"] < 1:
                return "no_attempts", None
            self.conn.execute("UPDATE users SET spins_used=spins_used+1 WHERE user_id=?", (user_id,))
            self.conn.execute(
                "INSERT OR REPLACE INTO promos(user_id, code, issued_at, expires_at, reminded) VALUES (?,?,?,?,0)",
                (user_id, code, now, now + PROMO_TTL))
            return "ok", {"user_id": user_id, "code": code, "issued_at": now, "expires_at": now + PROMO_TTL}

    def delete_promo(self, user_id: int, expires_at: int | None = None) -> bool:
        """Видаляє промокод. Якщо вказано expires_at — лише той самий (щоб старий таймер
        не видалив новий промокод). True — якщо щось було видалено."""
        with self.tx():
            if expires_at is None:
                existed = self.conn.execute("SELECT 1 FROM promos WHERE user_id=?", (user_id,)).fetchone()
                self.conn.execute("DELETE FROM promos WHERE user_id=?", (user_id,))
            else:
                existed = self.conn.execute("SELECT 1 FROM promos WHERE user_id=? AND expires_at=?",
                                            (user_id, expires_at)).fetchone()
                self.conn.execute("DELETE FROM promos WHERE user_id=? AND expires_at=?", (user_id, expires_at))
            return existed is not None

    def mark_reminded(self, user_id: int) -> None:
        with self.tx():
            self.conn.execute("UPDATE promos SET reminded=1 WHERE user_id=?", (user_id,))

    def list_promos(self) -> list[dict]:
        with self.lock:
            return self._all(f"SELECT {_sel(PROMO_COLS)} FROM promos", (), PROMO_COLS)

    # --- кошик -----------------------------------------------------------
    def set_cart(self, user_id: int, count: int) -> None:
        with self.tx():
            if count > 0:
                self.conn.execute("INSERT OR REPLACE INTO carts(user_id, item_count, updated_at) VALUES (?,?,?)",
                                  (user_id, count, int(time.time())))
            else:
                self.conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))

    def get_cart(self, user_id: int) -> dict | None:
        with self.lock:
            return self._one(f"SELECT {_sel(CART_COLS)} FROM carts WHERE user_id=?", (user_id,), CART_COLS)

    def list_carts(self) -> list[dict]:
        with self.lock:
            return self._all(f"SELECT {_sel(CART_COLS)} FROM carts", (), CART_COLS)

    # --- замовлення ------------------------------------------------------
    def consume_order(self, user_id: int, payload: str, total: int, promo_code: str | None,
                      details: str = "") -> tuple[int, int]:
        """Зберігає замовлення і В ТІЙ САМІЙ ТРАНЗАКЦІЇ видаляє промокод та кошик.
        Повертає (id замовлення, час створення)."""
        now = int(time.time())
        with self.tx():
            self.conn.execute(
                "INSERT INTO orders(user_id, created_at, total, promo_code, payload, details) VALUES (?,?,?,?,?,?)",
                (user_id, now, total, promo_code, payload, details or None))
            order_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.conn.execute("DELETE FROM promos WHERE user_id=?", (user_id,))
            self.conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
            return int(order_id), now

    def get_order(self, order_id: int) -> dict | None:
        with self.lock:
            return self._one(f"SELECT {_sel(ORDER_COLS)} FROM orders WHERE id=?", (int(order_id),), ORDER_COLS)

    def set_ttn(self, order_id: int, ttn: str) -> None:
        with self.tx():
            self.conn.execute("UPDATE orders SET ttn=? WHERE id=?", (ttn, int(order_id)))

    def user_has_ttn(self, user_id: int, ttn: str) -> bool:
        """Чи належить ця ТТН замовленню користувача (щоб не відстежувати чужі посилки)."""
        with self.lock:
            return self.conn.execute("SELECT 1 FROM orders WHERE user_id=? AND ttn=? LIMIT 1",
                                     (user_id, ttn)).fetchone() is not None

    def get_user_orders(self, user_id: int, limit: int = 20) -> list[dict]:
        """Історія замовлень користувача: від нових до старих."""
        with self.lock:
            return self._all(
                f"SELECT {_sel(ORDER_COLS)} FROM orders WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (user_id, int(limit)), ORDER_COLS)


async def dbrun(fn, *args):
    """Виконує метод БД в окремому потоці бази (fn — метод екземпляра Database)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(fn.__self__.executor, fn, *args)


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
    return {
        "subtotal": subtotal, "discount": discount,
        "free_ship": free_ship, "total": subtotal - discount, "units": len(units),
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


def order_view(row: dict) -> dict:
    """Замовлення з БД → зручний для Mini App / чату вигляд."""
    details = None
    if row.get("details"):
        try:
            details = json.loads(row["details"])
        except (TypeError, ValueError):
            details = None
    items, payment = [], "cod"
    if details:
        items = [{"name": str(i.get("name", "")), "qty": int(i.get("qty", 1)), "sum": int(i.get("sum", 0))}
                 for i in details.get("items", [])]
        payment = details.get("payment", "cod")
    else:  # старі замовлення без details: відновлюємо з вихідного JSON
        try:
            data = json.loads(row["payload"])
            for entry in data.get("items", []):
                product = CATALOG.get(str(entry.get("id")))
                qty = max(1, min(int(entry.get("q", 1)), MAX_QTY))
                if product:
                    items.append({"name": product["name"], "qty": qty, "sum": product["price"] * qty})
            payment = data.get("payment", "cod")
        except Exception:
            pass
    if payment not in PAYMENTS:
        payment = "cod"
    return {
        "id": row["id"], "no": order_number(row["id"], row["created_at"]), "ts": row["created_at"],
        "date": fmt_dt(row["created_at"]), "items": items, "total": row["total"], "promo": row["promo_code"],
        "payment": payment, "payment_label": PAYMENTS[payment], "payment_short": PAYMENT_SHORT[payment],
        "ttn": row.get("ttn") or None,
    }


# ----------------------------------------------------------------------
# Нова Пошта: відстеження посилки за ТТН (метод getStatusDocuments)
# ----------------------------------------------------------------------
NP_URL = "https://api.novaposhta.ua/v2.0/json/"
NP_CACHE_TTL = 60                      # секунд: не смикаємо API Нової Пошти частіше за раз на хвилину на ТТН
_np_cache: dict[str, tuple[float, dict]] = {}

# Код статусу Нової Пошти -> (емодзі, зрозумілий текст). Невідомий код — показуємо текст самої Нової Пошти.
NP_STATUS = {
    "1":   ("🕐", "Очікує надходження від відправника"),
    "3":   ("❓", "ТТН ще не з’явилась у системі Нової Пошти"),
    "4":   ("🚚", "Відправлення в дорозі"),
    "41":  ("🚚", "Відправлення в дорозі"),
    "5":   ("🚚", "Прямує до міста одержувача"),
    "6":   ("🚚", "Доставляється у відділення"),
    "7":   ("📬", "Прибуло у відділення"),
    "8":   ("📬", "Прибуло в поштомат"),
    "9":   ("✅", "Отримано"),
    "10":  ("✅", "Отримано"),
    "11":  ("✅", "Отримано"),
    "12":  ("📦", "Комплектується до відправлення"),
    "101": ("🚚", "Прямує до одержувача"),
    "102": ("↩️", "Відмова від отримання — створено повернення"),
    "103": ("↩️", "Відмова одержувача"),
}
NP_RECEIVED = {"9", "10", "11"}


def _np_date(value: object) -> str:
    """'30-12-2026 15:20:00' або '2026-12-30 15:20:00' -> '30.12.2026'."""
    text = str(value or "")
    m = re.match(r"(\d{2})[-.](\d{2})[-.](\d{4})", text)
    if m:
        return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else ""


def parse_np_response(data: object) -> dict:
    """Розбирає відповідь getStatusDocuments у короткий словник для Mini App."""
    if not isinstance(data, dict) or not data.get("success"):
        return {"ok": False, "error": "np_error"}
    rows = data.get("data") or []
    info = rows[0] if rows and isinstance(rows[0], dict) else None
    if not info:
        return {"ok": False, "error": "np_error"}
    code = str(info.get("StatusCode", "")).strip()
    np_text = str(info.get("Status", "")).strip()
    emoji, status = NP_STATUS.get(code, ("📦", np_text or "Статус уточнюється"))
    received = code in NP_RECEIVED
    date = _np_date(info.get("RecipientDateTime")) if received else ""
    date = date or _np_date(info.get("ScheduledDeliveryDate"))
    return {
        "ok": True, "number": str(info.get("Number", "")), "code": code, "emoji": emoji,
        "status": status, "np_status": np_text, "received": received,
        "city": str(info.get("CityRecipient", "") or "").strip(),
        "warehouse": str(info.get("WarehouseRecipient", "") or "").strip(),
        "delivery_date": date,
    }


async def np_track(ttn: str) -> dict:
    """Запитує статус посилки в Нової Пошти. Результат кешується на хвилину."""
    now = time.time()
    cached = _np_cache.get(ttn)
    if cached and now - cached[0] < NP_CACHE_TTL:
        return cached[1]
    payload = {
        "apiKey": NP_API_KEY,
        "modelName": "TrackingDocument",
        "calledMethod": "getStatusDocuments",
        "methodProperties": {"Documents": [{"DocumentNumber": ttn, "Phone": ""}]},
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(NP_URL, json=payload) as resp:
                data = await resp.json(content_type=None)
    except Exception as exc:
        log.warning("Нова Пошта недоступна (ТТН %s): %s", ttn, exc)
        return {"ok": False, "error": "np_unavailable"}
    result = parse_np_response(data)
    if result["ok"]:
        if len(_np_cache) > 500:
            _np_cache.clear()
        _np_cache[ttn] = (now, result)
    else:
        log.warning("Нова Пошта повернула помилку для ТТН %s: %s", ttn, str(data)[:300])
    return result


TTN_RE = re.compile(r"\d{10,14}")


def parse_order_ref(ref: str) -> int | None:
    """'VD-1310-5' або '5' -> 5 (id замовлення)."""
    m = re.fullmatch(r"(?:VD-\d{4}-)?(\d{1,9})", ref.strip(), flags=re.IGNORECASE)
    return int(m.group(1)) if m else None


def promo_notes(order: dict) -> list[str]:
    promo = order["promo"]
    notes = []
    if promo == "3FOR2":
        notes.append(f"✨ Акція 1+1=3: безкоштовним зараховано найдешевший товар у кожній трійці (−{money(order['discount'])}).")
        if order["units"] < 3:
            notes.append("ℹ️ Акція не спрацювала: у замовленні менше трьох товарів.")
    elif promo == "SECOND20" and order["units"] < 2:
        notes.append("ℹ️ SECOND20 не спрацював: у замовленні лише один товар.")
    elif promo == "FREESHIP600":
        if order["free_ship"]:
            notes.append("🚚 FREESHIP600: безкоштовна доставка — відправлення Новою Поштою оплачуєте ви.")
        else:
            notes.append(f"ℹ️ FREESHIP600 не спрацював: сума товарів менша за {money(FREE_SHIP_FROM)}. Доставку оплачує клієнт.")
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
    lines.append(f"<b>Разом до сплати: {money(order['total'])}</b> (без вартості доставки)")
    if order["payment"] == "cod":
        pre = min(PREPAYMENT, order["total"])
        lines.append(f"Передплата: {money(pre)} (очікується), при отриманні: {money(order['total'] - pre)}")
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
    total = order["total"]
    text = (
        f"✅ <b>Дякуємо за замовлення {order_no}!</b>\n\n"
        f"Сума замовлення: <b>{money(total)}</b>\n"
        f"Доставка: {esc(c['city'])}, відділення Нової Пошти №{esc(c['branch'])} — "
        f"{'безкоштовна 🚚' if order['free_ship'] else 'за тарифом Нової Пошти'}\n"
        f"Оплата: {PAYMENTS[order['payment']]}\n"
    )
    if order["payment"] == "iban":
        text += (
            f"\n💳 <b>Повна сума до сплати: {money(total)}</b>\n"
            f"Реквізити для оплати:\n<code>{esc(PAYMENT_DETAILS)}</code>\n"
            f"У призначенні вкажіть номер замовлення {order_no}.\n"
        )
    else:
        pre = min(PREPAYMENT, total)
        text += (
            f"\n💳 Щоб ми відправили замовлення, внесіть <b>передплату {money(pre)}</b> на реквізити:\n"
            f"<code>{esc(PAYMENT_DETAILS)}</code>\n"
            f"У призначенні вкажіть номер замовлення {order_no}.\n"
            f"Решту суми — <b>{money(total - pre)}</b> — сплатите при отриманні у відділенні Нової Пошти.\n"
        )
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


@router.message(Command("give"), F.from_user.id == ADMIN_ID)
async def cmd_give(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """Адмін видає прокрути:  /give <ID або @username> [кількість]   (за замовчуванням 1; мінус — забрати)."""
    parts = (command.args or "").split()
    if not parts:
        await message.answer(
            "🎁 <b>Видати прокрути</b>\n\n"
            "<code>/give 123456789</code> — +1 спроба\n"
            "<code>/give @username 3</code> — +3 спроби\n"
            "<code>/give 123456789 -1</code> — забрати одну\n\n"
            "Перевірити людину: <code>/spins @username</code>\n"
            "Користувач має хоча б раз натиснути /start у боті.")
        return
    try:
        amount = int(parts[1]) if len(parts) > 1 else 1
    except ValueError:
        await message.answer("Кількість має бути числом, напр. <code>/give @username 3</code>")
        return
    if amount == 0 or abs(amount) > 50:
        await message.answer("Вкажіть кількість від 1 до 50 (або від −1 до −50).")
        return
    target = await dbrun(db.find_user, parts[0])
    if not target:
        await message.answer("Користувача не знайдено. Він має хоча б раз натиснути /start у боті.")
        return

    left = await dbrun(db.add_spins, target["user_id"], amount)
    who = f"{esc(target['first_name'] or '—')} (ID <code>{target['user_id']}</code>)"
    report = f"✅ {who}: {amount:+d} {plural(abs(amount), 'спроба', 'спроби', 'спроб')}. Тепер доступно: <b>{left}</b>."
    if amount > 0:
        try:
            await bot.send_message(
                target["user_id"],
                f"🎁 Вам нараховано <b>+{amount}</b> {plural(amount, 'спробу', 'спроби', 'спроб')} колеса фортуни!\n"
                "Відкрийте магазин кнопкою внизу й крутіть.",
                reply_markup=webapp_keyboard(target["user_id"]))
        except TelegramAPIError:
            report += "\n⚠️ Повідомити користувача не вдалося (можливо, він заблокував бота), але спроби нараховано."
    await message.answer(report)


@router.message(Command("myorders"))
async def cmd_myorders(message: Message, db: Database) -> None:
    """Користувач дивиться свої останні замовлення прямо в чаті."""
    rows = await dbrun(db.get_user_orders, message.from_user.id, 5)
    if not rows:
        await message.answer("🧾 У вас поки немає замовлень.\nВідкрийте магазин кнопкою внизу — і оформіть перше!")
        return
    blocks = []
    for row in rows:
        o = order_view(row)
        items = "\n".join(f"• {esc(i['name'])} × {i['qty']}" for i in o["items"]) or "• —"
        promo = f"\n🎟 {esc(o['promo'])}" if o["promo"] else ""
        ttn = f"\n📦 ТТН: <code>{esc(o['ttn'])}</code>" if o.get("ttn") else ""
        blocks.append(f"<b>{o['no']}</b> · {o['date']}\n{items}{promo}{ttn}\n"
                      f"Разом: <b>{money(o['total'])}</b> · {esc(o['payment_short'])}")
    await message.answer("🧾 <b>Ваші останні замовлення</b>\n\n" + "\n\n".join(blocks))


@router.message(Command("setttn"), F.from_user.id == ADMIN_ID)
async def cmd_setttn(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """Адмін зберігає ТТН і клієнт одразу отримує сповіщення:  /setttn <номер замовлення або id> <ТТН>"""
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer(
            "📦 <b>Додати ТТН до замовлення</b>\n\n"
            "<code>/setttn VD-1310-5 20450123456789</code>\n"
            "<code>/setttn 5 20450123456789</code>\n\n"
            "Номер замовлення (VD-…) є в повідомленні про замовлення. "
            "Клієнт отримає сповіщення і побачить ТТН та статус у «Мої замовлення».")
        return
    order_id = parse_order_ref(parts[0])
    ttn = re.sub(r"\s+", "", parts[1])
    if order_id is None:
        await message.answer("Не розумію номер замовлення. Приклад: <code>VD-1310-5</code> або <code>5</code>")
        return
    if not TTN_RE.fullmatch(ttn):
        await message.answer("ТТН має містити від 10 до 14 цифр, без літер і пробілів.")
        return
    order = await dbrun(db.get_order, order_id)
    if not order:
        await message.answer("Замовлення з таким номером не знайдено.")
        return
    no = order_number(order["id"], order["created_at"])
    if order["ttn"] == ttn:
        await message.answer(f"ℹ️ Для {no} ця ТТН уже збережена, клієнта повторно не сповіщаю.")
        return

    changed = bool(order["ttn"])
    await dbrun(db.set_ttn, order_id, ttn)
    report = f"✅ ТТН <code>{ttn}</code> збережено для {no}."
    try:
        head = "📦 <b>ТТН вашого замовлення оновлено!</b>" if changed else "📦 <b>Ваше замовлення відправлено!</b>"
        await bot.send_message(
            order["user_id"],
            f"{head}\nТТН: <code>{ttn}</code>\n"
            "Відстежуйте статус у розділі «Мої замовлення».",
            reply_markup=webapp_keyboard(order["user_id"], "📦 Мої замовлення", open="orders"))
        report += "\nКлієнту надіслано сповіщення."
    except TelegramAPIError:
        report += "\n⚠️ Сповістити клієнта не вдалося (можливо, він заблокував бота), але ТТН збережено."
    await message.answer(report)


@router.message(Command("giveall"), F.from_user.id == ADMIN_ID)
async def cmd_giveall(message: Message, command: CommandObject, db: Database) -> None:
    """Акція: додати спроби ВСІМ користувачам:  /giveall <кількість>"""
    try:
        amount = int((command.args or "").split()[0])
    except (ValueError, IndexError):
        await message.answer("Використання: <code>/giveall 1</code> — +1 спроба кожному користувачу бота.")
        return
    if amount == 0 or abs(amount) > 20:
        await message.answer("Вкажіть кількість від 1 до 20 (або від −1 до −20).")
        return
    count = await dbrun(db.add_spins_all, amount)
    await message.answer(f"✅ Нараховано {amount:+d} {plural(abs(amount), 'спробу', 'спроби', 'спроб')} "
                         f"усім користувачам: <b>{count}</b>.\nПобачать нове значення, коли заново відкриють Mini App.")


@router.message(Command("spins"), F.from_user.id == ADMIN_ID)
async def cmd_spins(message: Message, command: CommandObject, db: Database) -> None:
    """Адмін дивиться стан користувача:  /spins <ID або @username>"""
    if not command.args:
        await message.answer("Використання: <code>/spins 123456789</code> або <code>/spins @username</code>")
        return
    user = await dbrun(db.find_user, command.args.split()[0])
    if not user:
        await message.answer("Користувача не знайдено.")
        return
    promo = await dbrun(db.get_active_promo, user["user_id"])
    promo_line = (f"{promo['code']} (ще {human_left(promo['expires_at'] - time.time())})" if promo else "немає")
    await message.answer(
        f"👤 {esc(user['first_name'] or '—')} (ID <code>{user['user_id']}</code>, "
        f"{('@' + esc(user['username'])) if user['username'] else 'без username'})\n"
        f"Спроб усього: {user['spins_total']}, використано: {user['spins_used']}, "
        f"<b>доступно: {max(0, user['spins_total'] - user['spins_used'])}</b>\n"
        f"Запросив друзів: {user['referrals']}\n"
        f"Активний промокод: {promo_line}")


@router.message(Command("backup"), F.from_user.id == ADMIN_ID)
async def cmd_backup(message: Message, db: Database) -> None:
    """Лише для адміна: надсилає файл бази (страховка на безкоштовному Render)."""
    if db.remote:
        await message.answer("☁️ База в хмарі Turso — окрема резервна копія не потрібна. "
                             "Дані зберігаються на їхньому боці й не зникають при перезапуску.")
        return
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
    details = json.dumps({
        "items": [{"name": it["name"], "qty": it["qty"], "price": it["price"], "sum": it["sum"]} for it in order["items"]],
        "payment": order["payment"], "subtotal": order["subtotal"], "discount": order["discount"],
        "total": order["total"], "free_ship": order["free_ship"],
    }, ensure_ascii=False)
    order_id, created_at = await dbrun(db.consume_order, user.id, message.web_app_data.data,
                                       order["total"], promo, details)
    scheduler.cancel_all(user.id)  # таймери згорання і нагадування більше не потрібні
    order_no = order_number(order_id, created_at)
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


async def api_orders(request: web.Request) -> web.Response:
    """Історія замовлень користувача для Mini App (від нових до старих)."""
    db: Database = request.app["db"]
    uid = auth_user(request.query.get("uid"), request.query.get("sig"))
    rows = await dbrun(db.get_user_orders, uid, 20)
    return web.json_response({"ok": True, "orders": [order_view(r) for r in rows]})


async def api_track(request: web.Request) -> web.Response:
    """Статус посилки Нової Пошти для Mini App. Відстежувати можна лише ТТН зі СВОЇХ замовлень."""
    db: Database = request.app["db"]
    uid = auth_user(request.query.get("uid"), request.query.get("sig"))
    ttn = re.sub(r"\s+", "", request.query.get("ttn", ""))
    if not TTN_RE.fullmatch(ttn):
        raise web.HTTPBadRequest(text="bad ttn")
    if not await dbrun(db.user_has_ttn, uid, ttn):
        return web.json_response({"ok": False, "error": "unknown_ttn"}, status=404)
    return web.json_response(await np_track(ttn))


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
    app.router.add_get("/api/orders", api_orders)
    app.router.add_get("/api/track", api_track)
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
    log.info("База даних: %s", "Turso (хмара)" if db.remote else f"локальний SQLite ({DB_PATH}) — на Render Free стирається при перезапуску")

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
