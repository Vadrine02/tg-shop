"""
Telegram-бот для Web App магазину аксесуарів (aiogram 3.7+).

Встановлення:   pip install -U aiogram
Запуск:         python bot.py

ВАЖЛИВО: Telegram.WebApp.sendData() працює ТІЛЬКИ якщо Web App відкрито
через кнопку reply-клавіатури (KeyboardButton). Якщо відкрити його через
Inline-кнопку, sendData() нічого не відправить — це обмеження Telegram.
Тому нижче використано KeyboardButton із web_app.
"""

import asyncio
import html
import json
import logging
import os
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo

# ======================================================================
# НАЛАШТУВАННЯ — ВСТАВТЕ СВОЇ ДАНІ
# ======================================================================

# 1) Токен бота від @BotFather.
#    Краще задати змінною середовища BOT_TOKEN, але можна вставити прямо сюди.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8786502270:AAGwuMug8AyV9DHu1pDJFM2851JxE_h2iE4")

# 2) Ваш Telegram ID (число). Дізнатись можна в боті @userinfobot.
#    На цей ID приходитимуть замовлення. Не забудьте натиснути /start у своєму боті.
ADMIN_ID = int(os.getenv("ADMIN_ID", "899013976"))

# 3) Посилання на index.html (HTTPS обов'язково!).
#    Для GitHub Pages: https://ВАШ_НІК.github.io/НАЗВА_РЕПОЗИТОРІЮ/
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://vadrine02.github.io/tg-shop/")

# ----------------------------------------------------------------------
# Каталог і акції. Мають збігатися з PRODUCTS / CONFIG у index.html.
# Ціни береться ТУТ, а не з клієнта — так їх неможливо підмінити.
# ----------------------------------------------------------------------
CATALOG = {
    "bag": {"name": "Сумка", "price": 2490},
    "wallet": {"name": "Гаманець", "price": 890},
    "watch": {"name": "Годинник", "price": 3290},
    "belt": {"name": "Ремінь", "price": 690},
}

PROMOS = {
    "ACCESS10": "Знижка 10%",
    "X2BONUS": "1+1=3",
    "FREESHIP": "Безкоштовна доставка",
    "GIFT": "Подарунок-сюрприз",
}

DELIVERY_FEE = 70        # ₴, має збігатися з CONFIG.DELIVERY_FEE у index.html
DISCOUNT_PERCENT = 10    # для ACCESS10
MAX_QTY = 20

# ======================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("shop-bot")

router = Router()


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₴"


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


# ----------------------------------------------------------------------
# /start
# ----------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛍 Відкрити магазин", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True,
        is_persistent=True,
    )
    name = esc(message.from_user.first_name) if message.from_user else "друже"
    await message.answer(
        f"Привіт, <b>{name}</b>! 👋\n\n"
        "Ласкаво просимо до магазину аксесуарів.\n"
        "Натисніть кнопку нижче, крутніть <b>Колесо фортуни</b> та заберіть свій бонус 🎁",
        reply_markup=keyboard,
    )


# ----------------------------------------------------------------------
# Парсинг та перевірка замовлення
# ----------------------------------------------------------------------
def clean_text(value: object, limit: int = 100) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_order(raw: str) -> dict:
    """Перевіряє JSON з Web App і перераховує суму на боці сервера."""
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

    if not (name and city and branch and len([c for c in phone if c.isdigit()]) >= 10):
        raise ValueError("Не всі дані клієнта заповнені")

    items = []
    subtotal = 0
    for entry in data.get("items") or []:
        product = CATALOG.get(str(entry.get("id")))
        try:
            qty = int(entry.get("q", 0))
        except (TypeError, ValueError):
            continue
        if not product or qty < 1:
            continue
        qty = min(qty, MAX_QTY)
        line_total = product["price"] * qty
        subtotal += line_total
        items.append({"name": product["name"], "qty": qty, "price": product["price"], "sum": line_total})

    if not items:
        raise ValueError("Порожній кошик")

    promo = str(data.get("promo") or "").upper()
    promo = promo if promo in PROMOS else None

    discount = round(subtotal * DISCOUNT_PERCENT / 100) if promo == "ACCESS10" else 0
    delivery = 0 if promo == "FREESHIP" else DELIVERY_FEE
    total = subtotal - discount + delivery

    client_total = data.get("total")
    if client_total != total:
        log.warning("Сума з клієнта (%s) не збігається з розрахунком (%s)", client_total, total)

    return {
        "name": name, "phone": phone, "city": city, "branch": branch,
        "items": items, "subtotal": subtotal, "discount": discount,
        "delivery": delivery, "total": total, "promo": promo,
    }


def promo_note(promo: str | None) -> str:
    if promo == "X2BONUS":
        return "\n⚠️ <i>Акція 1+1=3: додайте третій товар у подарунок.</i>"
    if promo == "GIFT":
        return "\n⚠️ <i>Не забудьте покласти подарунок-сюрприз.</i>"
    return ""


def admin_text(order: dict, order_id: str, message: Message) -> str:
    user = message.from_user
    username = f"@{esc(user.username)}" if user and user.username else "—"
    full_name = esc(user.full_name) if user else "—"
    user_link = f'<a href="tg://user?id={user.id}">{full_name}</a>' if user else "—"

    lines = [
        f"🛍 <b>Нове замовлення #{order_id}</b>",
        "",
        "👤 <b>Клієнт</b>",
        f"ПІБ: {esc(order['name'])}",
        f"Телефон: <code>{esc(order['phone'])}</code>",
        f"Місто: {esc(order['city'])}",
        f"Нова Пошта: відділення №{esc(order['branch'])}",
        f"Telegram: {user_link} ({username}), ID <code>{user.id if user else '—'}</code>",
        "",
        "📦 <b>Товари</b>",
    ]
    for i, it in enumerate(order["items"], 1):
        lines.append(f"{i}. {esc(it['name'])} × {it['qty']} — {money(it['sum'])}")

    lines += ["", "💳 <b>Оплата</b>", f"Товари: {money(order['subtotal'])}"]
    if order["discount"]:
        lines.append(f"Знижка {DISCOUNT_PERCENT}%: −{money(order['discount'])}")
    lines.append(f"Доставка: {money(order['delivery']) if order['delivery'] else 'безкоштовно'}")
    lines.append(f"<b>Разом до сплати: {money(order['total'])}</b>")

    if order["promo"]:
        lines += ["", f"🎟 Промокод клієнта: <b>{order['promo']}</b> ({PROMOS[order['promo']]})"]
    else:
        lines += ["", "🎟 Промокод: немає"]

    return "\n".join(lines) + promo_note(order["promo"])


def client_text(order: dict, order_id: str) -> str:
    return (
        f"✅ <b>Дякуємо за замовлення #{order_id}!</b>\n\n"
        f"Сума до сплати: <b>{money(order['total'])}</b>\n"
        f"Доставка: {esc(order['city'])}, відділення Нової Пошти №{esc(order['branch'])}\n\n"
        "Менеджер зв'яжеться з вами найближчим часом для підтвердження. 💜"
    )


# ----------------------------------------------------------------------
# Отримання даних із Web App
# ----------------------------------------------------------------------
@router.message(F.web_app_data)
async def on_web_app_data(message: Message, bot: Bot) -> None:
    try:
        order = parse_order(message.web_app_data.data)
    except ValueError as exc:
        log.warning("Невалідне замовлення від %s: %s", message.from_user.id, exc)
        await message.answer("😕 Не вдалося обробити замовлення. Відкрийте магазин і спробуйте ще раз.")
        return

    order_id = f"AX-{int(time.time()) % 1_000_000:06d}"
    log.info("Замовлення %s від %s на суму %s", order_id, message.from_user.id, order["total"])

    # Адміну
    try:
        await bot.send_message(ADMIN_ID, admin_text(order, order_id, message))
    except TelegramAPIError:
        log.exception("Не вдалося надіслати замовлення %s адміну (ADMIN_ID=%s)", order_id, ADMIN_ID)

    # Клієнту
    await message.answer(client_text(order, order_id))


# ----------------------------------------------------------------------
async def main() -> None:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущено")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
