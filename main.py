import os
import re
import base64
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
import httpx
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
from aiogram.types.input_file import BufferedInputFile


# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()

CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Acesso Canal VIP").strip()
PRICE = float(os.getenv("PRICE", "29.90"))
CURRENCY = os.getenv("CURRENCY", "BRL").strip()

SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))  # duração da assinatura (dias)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não configurado")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("MP_ACCESS_TOKEN não configurado")

DB_PATH = os.getenv("DB_PATH", "db.sqlite3")


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
  mp_payment_id TEXT PRIMARY KEY,
  telegram_id INTEGER NOT NULL,
  email TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  approved_at TEXT,
  expires_at TEXT
);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)

        # migração simples (caso a tabela antiga não tenha as colunas)
        try:
            await db.execute("ALTER TABLE payments ADD COLUMN approved_at TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE payments ADD COLUMN expires_at TEXT")
        except Exception:
            pass

        await db.commit()

async def db_insert_payment(mp_payment_id: str, telegram_id: int, email: str, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO payments(mp_payment_id, telegram_id, email, status, created_at) VALUES (?,?,?,?,?)",
            (mp_payment_id, telegram_id, email, status, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()

async def db_get_payment(mp_payment_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT mp_payment_id, telegram_id, email, status, created_at, approved_at, expires_at FROM payments WHERE mp_payment_id=?",
            (mp_payment_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        return row

async def db_update_status(mp_payment_id: str, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE payments SET status=? WHERE mp_payment_id=?",
            (status, mp_payment_id)
        )
        await db.commit()

async def db_mark_approved(mp_payment_id: str):
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SUB_DAYS)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE payments SET status=?, approved_at=?, expires_at=? WHERE mp_payment_id=?",
            ("approved", now.isoformat(), expires.isoformat(), mp_payment_id)
        )
        await db.commit()

async def db_get_latest_by_telegram(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT mp_payment_id, telegram_id, email, status, created_at, approved_at, expires_at
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


# =========================
# Mercado Pago helpers
# =========================
MP_BASE = "https://api.mercadopago.com"

def is_valid_email(email: str) -> bool:
    email = email.strip()
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))

async def mp_create_pix_payment(amount: float, description: str, payer_email: str, external_ref: str):
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": external_ref,
    }
    payload = {
        "transaction_amount": round(float(amount), 2),
        "description": description,
        "payment_method_id": "pix",
        "payer": {"email": payer_email},
        "external_reference": external_ref,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{MP_BASE}/v1/payments", headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    payment_id = str(data.get("id"))
    status = data.get("status", "unknown")

    poi = data.get("point_of_interaction", {}) or {}
    tx = poi.get("transaction_data", {}) or {}
    copia_e_cola = tx.get("qr_code", "")
    qr_base64 = tx.get("qr_code_base64", "")

    return payment_id, copia_e_cola, qr_base64, status

async def mp_get_payment(payment_id: str):
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{MP_BASE}/v1/payments/{payment_id}", headers=headers)
        r.raise_for_status()
        return r.json()


# =========================
# Telegram: states
# =========================
class BuyFlow(StatesGroup):
    waiting_email = State()


def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Comprar acesso (PIX)", callback_data="buy_pix")],
        [InlineKeyboardButton(text="📌 Minha assinatura", callback_data="my_sub")],
        [InlineKeyboardButton(text="📞 Suporte", callback_data="support")]
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
        f"✅ Bem-vindo!\n\n"
        f"Produto: {PRODUCT_NAME}\n"
        f"Valor: R$ {PRICE:.2f}\n"
        f"Duração: {SUB_DAYS} dias\n\n"
        f"Toque em **Comprar** para gerar seu PIX.",
        reply_markup=kb_main(),
        parse_mode="Markdown"
    )

@dp.message(Command("planos"))
async def cmd_planos(msg: Message):
    await msg.answer(
        f"📌 Plano disponível:\n\n"
        f"• {PRODUCT_NAME} — R$ {PRICE:.2f} — {SUB_DAYS} dias\n",
        reply_markup=kb_main()
    )

@dp.message(Command("get_channel_id"))
async def cmd_get_channel_id(msg: Message):
    await msg.answer(
        "Para eu descobrir o ID do seu canal/grupo:\n\n"
        "1) Vá no seu canal/grupo e encaminhe (forward) uma mensagem dele aqui pra mim.\n"
        "2) Eu vou responder com o CHANNEL_ID.\n\n"
        "⚠️ Tem que ser mensagem encaminhada do canal/grupo (não print)."
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
        f"Produto: {PRODUCT_NAME}\nValor: R$ {PRICE:.2f}\n\nClique em Comprar para gerar o PIX.",
        reply_markup=kb_main()
    )
    await c.answer()

@dp.callback_query(F.data == "buy_pix")
async def cb_buy_pix(c: CallbackQuery, state: FSMContext):
    await state.set_state(BuyFlow.waiting_email)
    await c.message.answer(
        "Antes de gerar o PIX, me envie seu **e-mail** (obrigatório pelo Mercado Pago).\n\n"
        "Exemplo: nome@gmail.com",
        reply_markup=kb_back()
    )
    await c.answer()

@dp.callback_query(F.data == "my_sub")
async def cb_my_sub(c: CallbackQuery):
    telegram_id = c.from_user.id
    row = await db_get_latest_by_telegram(telegram_id)

    if not row:
        await c.message.answer("Você ainda não tem assinatura. Clique em 💳 Comprar acesso.")
        await c.answer()
        return

    mp_id, tg_id, email, status, created_at, approved_at, expires_at = row

    if not expires_at:
        await c.message.answer(
            "⏳ Encontrei um pedido seu, mas sua assinatura ainda não está ativa.\n"
            "Se você já pagou, aguarde um pouco e tente novamente."
        )
        await c.answer()
        return

    exp = datetime.fromisoformat(expires_at)
    now = datetime.now(timezone.utc)

    if now < exp:
        restante = exp - now
        dias = max(restante.days, 0)

        invite_link = await create_invite_link()

        if not invite_link:
            await c.message.answer(
                "✅ Assinatura ativa, mas não consegui gerar o link agora.\n"
                "Verifique se o bot é ADMIN e tem permissão de convidar usuários."
            )
            await c.answer()
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Entrar no VIP", url=invite_link)]
        ])

        await c.message.answer(
            f"✅ Assinatura *ativa*\n"
            f"📅 Expira em: *{exp.astimezone().strftime('%d/%m/%Y %H:%M')}*\n"
            f"⏳ Restam: *{dias} dia(s)*\n\n"
            f"Se você caiu, clique no botão abaixo para entrar novamente 👇",
            parse_mode="Markdown",
            reply_markup=kb
        )
    else:
        await c.message.answer(
            f"❌ Assinatura *expirada*\n"
            f"📅 Expirou em: *{exp.astimezone().strftime('%d/%m/%Y %H:%M')}*\n\n"
            "Clique em 💳 Comprar acesso para renovar.",
            parse_mode="Markdown"
        )

    await c.answer()

@dp.message(BuyFlow.waiting_email)
async def on_email(msg: Message, state: FSMContext):
    email = (msg.text or "").strip()
    if not is_valid_email(email):
        await msg.answer("❌ E-mail inválido. Envie um e-mail válido (ex: nome@gmail.com).")
        return

    telegram_id = msg.from_user.id
    external_ref = f"tg{telegram_id}-{int(datetime.now(timezone.utc).timestamp())}"

    await msg.answer("⏳ Gerando seu PIX...")

    try:
        mp_payment_id, copia, qr_b64, status = await mp_create_pix_payment(
            amount=PRICE,
            description=PRODUCT_NAME,
            payer_email=email,
            external_ref=external_ref
        )
        await db_insert_payment(mp_payment_id, telegram_id, email, status)
    except Exception as e:
        await msg.answer(f"❌ Erro ao gerar cobrança. Tente novamente.\n\nDetalhe: {type(e).__name__}")
        await state.clear()
        return

    if qr_b64:
        try:
            img_bytes = base64.b64decode(qr_b64)
            photo = BufferedInputFile(img_bytes, filename="pix.png")
            await msg.answer_photo(photo, caption="📷 QR Code PIX")
        except Exception:
            pass

    if copia:
        await msg.answer(
            "✅ PIX gerado!\n\n"
            "📋 *Copia e Cola (PIX):*\n"
            f"`{copia}`\n\n"
            "Assim que o pagamento for aprovado, eu libero o acesso automaticamente. 🔓",
            parse_mode="Markdown"
        )
    else:
        await msg.answer(
            "✅ Cobrança criada! Aguarde a confirmação do pagamento.\n"
            f"ID do pagamento: {mp_payment_id}"
        )

    await state.clear()


# =========================
# Telegram: link de convite
# =========================
async def create_invite_link() -> Optional[str]:
    if not CHANNEL_ID:
        return None

    expire = datetime.now(timezone.utc) + timedelta(minutes=10)

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=int(CHANNEL_ID),
            member_limit=1,
            expire_date=expire
        )
        return invite.invite_link
    except Exception:
        return None


# =========================
# FastAPI: webhook Mercado Pago
# =========================
@app.post("/mp/webhook")
@app.get("/mp/webhook")
async def mp_webhook(request: Request):
    params = dict(request.query_params)

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    payment_id: Optional[str] = None
    topic = params.get("topic") or params.get("type") or payload.get("type")

    if params.get("id"):
        payment_id = params.get("id")
    else:
        data = payload.get("data") or {}
        if isinstance(data, dict) and data.get("id"):
            payment_id = str(data.get("id"))

    if not payment_id:
        return JSONResponse({"ok": True, "ignored": True})

    try:
        mp_data = await mp_get_payment(payment_id)
        status = mp_data.get("status", "unknown")
    except Exception:
        return JSONResponse({"ok": True, "error": "failed_to_fetch_payment"}, status_code=200)

    row = await db_get_payment(str(payment_id))
    if not row:
        return JSONResponse({"ok": True, "unknown_payment": True})

    _, telegram_id, _, old_status, _, _, _ = row

    if status != old_status:
        await db_update_status(str(payment_id), status)

    if status == "approved":
        # marca expiração só uma vez
        if old_status != "approved":
            await db_mark_approved(str(payment_id))

        invite_link = await create_invite_link()
        if invite_link:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Entrar no VIP", url=invite_link)]
            ])
            await bot.send_message(
                int(telegram_id),
                "✅ Pagamento aprovado!\n\n"
                "Clique no botão abaixo para entrar no VIP 👇",
                reply_markup=kb
            )
        else:
            await bot.send_message(
                int(telegram_id),
                "✅ Pagamento aprovado!\n\n"
                "⚠️ Não consegui gerar o link agora. Verifique se o bot é ADMIN e tem permissão de convidar usuários."
            )

    return JSONResponse({"ok": True, "status": status, "topic": topic})


# =========================
# Startup
# =========================
@app.on_event("startup")
async def on_startup():
    await db_init()
    asyncio.create_task(dp.start_polling(bot))

@app.get("/")
async def root():
    return {"ok": True, "service": "telegram-vip-bot"}
