import os
import re
import base64
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import aiosqlite
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage


# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Acesso Canal VIP").strip()
CURRENCY = os.getenv("CURRENCY", "BRL").strip()

# Planos
PRICE_30 = float(os.getenv("PRICE_30", "9.99"))
PRICE_LIFE = float(os.getenv("PRICE_LIFE", "19.99"))
SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))

# Kiwify (seus links)
KIWIFY_LINK_30 = os.getenv("KIWIFY_LINK_30", "https://pay.kiwify.com.br/TvLqICI").strip()
KIWIFY_LINK_LIFE = os.getenv("KIWIFY_LINK_LIFE", "https://pay.kiwify.com.br/PAd2mH9").strip()

# Webhook token (opcional, mas recomendado)
KIWIFY_WEBHOOK_TOKEN = os.getenv("KIWIFY_WEBHOOK_TOKEN", "").strip()

DB_PATH = os.getenv("DB_PATH", "db.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não configurado")


# =========================
# BOT + FASTAPI
# =========================
bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()


# =========================
# DB
# =========================
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_id INTEGER NOT NULL,
  email TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  approved_at TEXT,
  expires_at TEXT,
  plan TEXT NOT NULL
);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)

        # Migrações simples (caso o DB já exista antigo)
        for stmt in [
            "ALTER TABLE payments ADD COLUMN email TEXT",
            "ALTER TABLE payments ADD COLUMN approved_at TEXT",
            "ALTER TABLE payments ADD COLUMN expires_at TEXT",
            "ALTER TABLE payments ADD COLUMN plan TEXT",
            "ALTER TABLE payments ADD COLUMN id INTEGER",
        ]:
            try:
                await db.execute(stmt)
            except Exception:
                pass

        await db.commit()

async def db_create_pending(telegram_id: int, plan: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO payments(telegram_id, email, status, created_at, approved_at, expires_at, plan)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                telegram_id,
                None,
                "pending",
                datetime.now(timezone.utc).isoformat(),
                None,
                None,
                plan
            )
        )
        await db.commit()

async def db_attach_email_latest(telegram_id: int, email: str):
    async with aiosqlite.connect(DB_PATH) as db:
        # atualiza o registro mais recente pending/qualquer do usuário
        await db.execute(
            """
            UPDATE payments
            SET email=?
            WHERE id = (
              SELECT id FROM payments
              WHERE telegram_id=?
              ORDER BY created_at DESC
              LIMIT 1
            )
            """,
            (email, telegram_id)
        )
        await db.commit()

async def db_get_latest_by_telegram(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, telegram_id, email, status, created_at, approved_at, expires_at, plan
            FROM payments
            WHERE telegram_id=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (telegram_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        return row

async def db_get_latest_by_email(email: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, telegram_id, email, status, created_at, approved_at, expires_at, plan
            FROM payments
            WHERE email=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (email,)
        )
        row = await cur.fetchone()
        await cur.close()
        return row

async def db_mark_approved(row_id: int, plan: str):
    now = datetime.now(timezone.utc)
    if plan == "life":
        expires = None
    else:
        expires = now + timedelta(days=SUB_DAYS)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE payments
            SET status=?, approved_at=?, expires_at=?
            WHERE id=?
            """,
            (
                "approved",
                now.isoformat(),
                (expires.isoformat() if expires else None),
                row_id
            )
        )
        await db.commit()


# =========================
# Helpers
# =========================
def is_valid_email(email: str) -> bool:
    email = email.strip()
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


# =========================
# Telegram: states
# =========================
class BuyFlow(StatesGroup):
    waiting_email = State()


def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Assinar 30 dias (R$ {PRICE_30:.2f})", callback_data="buy_30")],
        [InlineKeyboardButton(text=f"💎 Vitalícia (R$ {PRICE_LIFE:.2f})", callback_data="buy_life")],
        [InlineKeyboardButton(text="📌 Minha assinatura", callback_data="my_sub")],
        [InlineKeyboardButton(text="📞 Suporte", callback_data="support")],
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Voltar", callback_data="back")]
    ])


# =========================
# Telegram: commands
# =========================
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    await msg.answer(
        f"🚀 *Bem-vindo ao acesso VIP!*\n\n"
        f"Aqui você entra para o *{PRODUCT_NAME}* e recebe:\n"
        f"✅ Conteúdo exclusivo\n"
        f"✅ Atualizações frequentes\n"
        f"✅ Acesso imediato após o pagamento\n\n"
        f"💳 *Escolha seu plano abaixo:*",
        parse_mode="Markdown",
        reply_markup=kb_main()
    )

@dp.message(Command("planos"))
async def cmd_planos(msg: Message):
    await msg.answer(
        f"💎 *PLANOS DISPONÍVEIS*\n\n"
        f"🗓 *30 dias de acesso* — R$ {PRICE_30:.2f}\n"
        f"♾ *Acesso vitalício* — R$ {PRICE_LIFE:.2f}\n\n"
        f"Pagamento via Kiwify com liberação automática 🔓",
        parse_mode="Markdown",
        reply_markup=kb_main()
    )

@dp.message(Command("get_channel_id"))
async def cmd_get_channel_id(msg: Message):
    await msg.answer(
        "Para eu descobrir o ID do seu canal:\n\n"
        "1) Vá no seu canal e encaminhe (forward) uma mensagem dele aqui pra mim.\n"
        "2) Eu vou responder com o CHANNEL_ID.\n\n"
        "⚠️ Tem que ser mensagem encaminhada do canal (não print)."
    )

@dp.message(F.forward_from_chat)
async def on_forwarded(msg: Message):
    chat = msg.forward_from_chat
    if not chat:
        return
    await msg.answer(
        f"✅ Achei!\n\n"
        f"Nome: {chat.title}\n"
        f"CHANNEL_ID: `{chat.id}`\n\n"
        f"Agora coloque esse valor no Render em ENV `CHANNEL_ID`.",
        parse_mode="Markdown"
    )


# =========================
# Telegram: callbacks
# =========================
@dp.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery):
    await c.message.answer("Suporte: fale com o admin do canal (você pode colocar seu @ aqui depois).")
    await c.answer()

@dp.callback_query(F.data == "back")
async def cb_back(c: CallbackQuery):
    await c.message.edit_text(
        "💳 *Escolha seu plano abaixo:*",
        parse_mode="Markdown",
        reply_markup=kb_main()
    )
    await c.answer()

@dp.callback_query(F.data.in_(["buy_30", "buy_life"]))
async def cb_choose_plan(c: CallbackQuery, state: FSMContext):
    plan = "30d" if c.data == "buy_30" else "life"
    label = "30 dias" if plan == "30d" else "Vitalícia"
    link = KIWIFY_LINK_30 if plan == "30d" else KIWIFY_LINK_LIFE

    # cria um "pedido pendente" no DB
    await db_create_pending(c.from_user.id, plan)

    # pede email para casar com o pagamento do webhook
    await state.update_data(plan=plan, link=link, label=label)
    await state.set_state(BuyFlow.waiting_email)

    await c.message.answer(
        f"🛒 Você escolheu: *{label}*\n\n"
        "✅ Para eu liberar automaticamente, me envie o *mesmo e-mail* que você vai usar no checkout da Kiwify.\n\n"
        "Exemplo: nome@gmail.com",
        parse_mode="Markdown",
        reply_markup=kb_back()
    )
    await c.answer()

@dp.message(BuyFlow.waiting_email)
async def on_email(msg: Message, state: FSMContext):
    email = (msg.text or "").strip()
    if not is_valid_email(email):
        await msg.answer("❌ E-mail inválido. Envie um e-mail válido (ex: nome@gmail.com).")
        return

    data = await state.get_data()
    link = data.get("link", KIWIFY_LINK_30)
    label = data.get("label", "30 dias")

    # salva email no último pedido do usuário
    await db_attach_email_latest(msg.from_user.id, email)

    await msg.answer(
        f"✅ Perfeito! Agora finalize o pagamento no link abaixo:\n\n"
        f"🛒 *Plano {label}*\n"
        f"🔗 {link}\n\n"
        "Assim que a Kiwify confirmar o pagamento, eu libero seu acesso automaticamente. 🚀",
        parse_mode="Markdown"
    )

    await state.clear()

@dp.callback_query(F.data == "my_sub")
async def cb_my_sub(c: CallbackQuery):
    telegram_id = c.from_user.id
    row = await db_get_latest_by_telegram(telegram_id)

    if not row:
        await c.message.answer("Você ainda não tem assinatura. Clique em 💳 Assinar para gerar o pagamento.")
        await c.answer()
        return

    _id, tg_id, email, status, created_at, approved_at, expires_at, plan = row

    if status != "approved":
        await c.message.answer(
            "⏳ Encontrei um pedido seu, mas a assinatura ainda não está ativa.\n"
            "Se você já pagou, aguarde alguns instantes e tente novamente."
        )
        await c.answer()
        return

    if plan == "life":
        await c.message.answer(
            "💎 Sua assinatura é *VITALÍCIA*.\n"
            "Vou gerar um link novo pra você entrar 👇",
            parse_mode="Markdown"
        )
        await grant_access(telegram_id)
        await c.answer()
        return

    if not expires_at:
        await c.message.answer(
            "⚠️ Sua assinatura consta como aprovada, mas não encontrei a data de expiração.\n"
            "Fale com o suporte."
        )
        await c.answer()
        return

    exp = datetime.fromisoformat(expires_at)
    now = datetime.now(timezone.utc)

    if now < exp:
        restante = exp - now
        dias = max(restante.days, 0)
        await c.message.answer(
            f"✅ Assinatura *ativa*\n"
            f"📅 Expira em: *{exp.astimezone().strftime('%d/%m/%Y %H:%M')}*\n"
            f"⏳ Restam: *{dias} dia(s)*\n\n"
            "Vou gerar um link novo pra você entrar 👇",
            parse_mode="Markdown"
        )
        await grant_access(telegram_id)
    else:
        await c.message.answer(
            f"❌ Assinatura *expirada*\n"
            f"📅 Expirou em: *{exp.astimezone().strftime('%d/%m/%Y %H:%M')}*\n\n"
            "Clique em 💳 Assinar 30 dias ou 💎 Vitalícia para renovar.",
            parse_mode="Markdown"
        )

    await c.answer()


# =========================
# Telegram: liberar acesso
# =========================
async def grant_access(telegram_id: int):
    if not CHANNEL_ID:
        await bot.send_message(
            telegram_id,
            "⚠️ Pagamento aprovado, mas o administrador ainda não configurou o CHANNEL_ID do canal."
        )
        return

    expire = datetime.now(timezone.utc) + timedelta(minutes=10)

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=int(CHANNEL_ID),
            member_limit=1,
            expire_date=expire
        )
        await bot.send_message(
            telegram_id,
            "✅ Aqui está seu link de acesso (1 uso / expira em 10 min):\n"
            f"{invite.invite_link}\n\n"
            "Se expirar, clique em 📌 Minha assinatura para gerar outro.",
        )
    except Exception:
        await bot.send_message(
            telegram_id,
            "⚠️ Não consegui criar o link.\n"
            "Verifique se o bot é ADMIN do canal e tem permissão de convidar usuários."
        )


# =========================
# FastAPI: webhook Kiwify
# =========================
@app.post("/kiwify/webhook")
async def kiwify_webhook(request: Request):
    # Se você configurou um token no webhook da Kiwify, valide aqui:
    if KIWIFY_WEBHOOK_TOKEN:
        token = request.headers.get("X-Webhook-Token") or request.headers.get("x-webhook-token") or ""
        if token.strip() != KIWIFY_WEBHOOK_TOKEN:
            return JSONResponse({"ok": False, "error": "invalid_token"}, status_code=401)

    data = {}
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": True})

    # Campos comuns (podem variar — mas isso já cobre o básico)
    status = (data.get("status") or "").lower()
    customer = data.get("customer") or {}
    email = (customer.get("email") or "").strip().lower()

    # Aceita somente aprovado/paid
    if status not in ("approved", "paid", "aprovado"):
        return JSONResponse({"ok": True, "ignored": True})

    if not email:
        return JSONResponse({"ok": True, "missing_email": True})

    row = await db_get_latest_by_email(email)
    if not row:
        return JSONResponse({"ok": True, "user_not_found": True})

    row_id, telegram_id, _email, old_status, created_at, approved_at, expires_at, plan = row

    # só marca aprovado se ainda não estava
    if old_status != "approved":
        await db_mark_approved(row_id, plan)

    # libera acesso
    await grant_access(int(telegram_id))

    return JSONResponse({"ok": True})


# =========================
# Healthcheck
# =========================
@app.get("/")
async def root():
    return {"ok": True, "service": "telegram-vip-bot"}


# =========================
# Startup
# =========================
@app.on_event("startup")
async def on_startup():
    await db_init()
    asyncio.create_task(dp.start_polling(bot))
