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
CURRENCY = os.getenv("CURRENCY", "BRL").strip()

# Preços e duração
PRICE_30 = float(os.getenv("PRICE_30", os.getenv("PRICE", "29.90")))
PRICE_LIFE = float(os.getenv("PRICE_LIFE", "149.90"))
SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))

DB_PATH = os.getenv("DB_PATH", "db.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não configurado")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("MP_ACCESS_TOKEN não configurado")


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
  expires_at TEXT,
  plan TEXT
);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)

        # Migrações simples (caso o DB já exista antigo)
        for stmt in [
            "ALTER TABLE payments ADD COLUMN approved_at TEXT",
            "ALTER TABLE payments ADD COLUMN expires_at TEXT",
            "ALTER TABLE payments ADD COLUMN plan TEXT",
        ]:
            try:
                await db.execute(stmt)
            except Exception:
                pass

        await db.commit()

async def db_insert_payment(mp_payment_id: str, telegram_id: int, email: str, status: str, plan: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO payments(
              mp_payment_id, telegram_id, email, status, created_at, approved_at, expires_at, plan
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                mp_payment_id,
                telegram_id,
                email,
                status,
                datetime.now(timezone.utc).isoformat(),
                None,
                None,
                plan
            )
        )
        await db.commit()

async def db_get_payment(mp_payment_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT mp_payment_id, telegram_id, email, status, created_at, approved_at, expires_at, plan
            FROM payments
            WHERE mp_payment_id=?
            """,
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

async def db_mark_approved(mp_payment_id: str, plan: str):
    now = datetime.now(timezone.utc)

    # Vitalícia: expires_at = NULL
    if plan == "life":
        expires = None
    else:
        expires = now + timedelta(days=SUB_DAYS)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE payments
            SET status=?, approved_at=?, expires_at=?
            WHERE mp_payment_id=?
            """,
            (
                "approved",
                now.isoformat(),
                (expires.isoformat() if expires else None),
                mp_payment_id
            )
        )
        await db.commit()

async def db_get_latest_by_telegram(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT mp_payment_id, telegram_id, email, status, created_at, approved_at, expires_at, plan
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
    """
    Cria pagamento PIX.
    Retorna: (payment_id, copia_e_cola, qr_base64, status)
    """
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
        f"✅ Bem-vindo!\n\n"
        f"Produto: {PRODUCT_NAME}\n\n"
        f"Escolha um plano abaixo 👇",
        reply_markup=kb_main()
    )

@dp.message(Command("planos"))
async def cmd_planos(msg: Message):
    await msg.answer(
        f"📌 Planos:\n\n"
        f"• 30 dias — R$ {PRICE_30:.2f}\n"
        f"• Vitalícia — R$ {PRICE_LIFE:.2f}\n",
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
        f"Escolha um plano abaixo 👇",
        reply_markup=kb_main()
    )
    await c.answer()

@dp.callback_query(F.data.in_(["buy_30", "buy_life"]))
async def cb_choose_plan(c: CallbackQuery, state: FSMContext):
    plan = "30d" if c.data == "buy_30" else "life"
    await state.update_data(plan=plan)
    await state.set_state(BuyFlow.waiting_email)

    texto = "30 dias" if plan == "30d" else "Vitalícia"
    await c.message.answer(
        f"Você escolheu: *{texto}*\n\n"
        "Agora me envie seu *e-mail* (obrigatório pelo Mercado Pago).",
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
    plan = data.get("plan", "30d")

    if plan == "life":
        amount = PRICE_LIFE
        description = f"{PRODUCT_NAME} (Vitalícia)"
        plan_label = "Vitalícia"
    else:
        amount = PRICE_30
        description = f"{PRODUCT_NAME} (30 dias)"
        plan_label = "30 dias"

    telegram_id = msg.from_user.id
    external_ref = f"tg{telegram_id}-{plan}-{int(datetime.now(timezone.utc).timestamp())}"

    await msg.answer(f"⏳ Gerando seu PIX ({plan_label})...")

    try:
        mp_payment_id, copia, qr_b64, status = await mp_create_pix_payment(
            amount=amount,
            description=description,
            payer_email=email,
            external_ref=external_ref
        )
        await db_insert_payment(mp_payment_id, telegram_id, email, status, plan)
    except Exception as e:
        await msg.answer(f"❌ Erro ao gerar cobrança. Tente novamente.\n\nDetalhe: {type(e).__name__}")
        await state.clear()
        return

    # Envia QR como imagem (se tiver)
    if qr_b64:
        try:
            img_bytes = base64.b64decode(qr_b64)
            photo = BufferedInputFile(img_bytes, filename="pix.png")
            await msg.answer_photo(photo, caption="📷 QR Code PIX")
        except Exception:
            pass

    # Envia copia e cola
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

@dp.callback_query(F.data == "my_sub")
async def cb_my_sub(c: CallbackQuery):
    telegram_id = c.from_user.id
    row = await db_get_latest_by_telegram(telegram_id)

    if not row:
        await c.message.answer("Você ainda não tem assinatura. Clique em 💳 Assinar para gerar o PIX.")
        await c.answer()
        return

    mp_id, tg_id, email, status, created_at, approved_at, expires_at, plan = row
    plan = plan or "30d"

    # Se ainda não foi aprovado
    if status != "approved":
        await c.message.answer(
            "⏳ Encontrei um pedido seu, mas a assinatura ainda não está ativa.\n"
            "Se você já pagou, aguarde alguns instantes e tente novamente."
        )
        await c.answer()
        return

    # Vitalícia
    if plan == "life":
        await c.message.answer(
            "💎 Sua assinatura é *VITALÍCIA*.\n"
            "Vou gerar um link novo pra você entrar 👇",
            parse_mode="Markdown"
        )
        await grant_access(telegram_id)
        await c.answer()
        return

    # Mensal: precisa ter expires_at
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
# Telegram: liberar acesso (link 1 uso / 10 min)
# =========================
async def grant_access(telegram_id: int):
    if not CHANNEL_ID:
        await bot.send_message(
            telegram_id,
            "⚠️ Pagamento aprovado, mas o administrador ainda não configurou o CHANNEL_ID do canal. "
            "Peça ao admin para configurar e depois solicite suporte."
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
# FastAPI: webhook Mercado Pago
# =========================
@app.post("/mp/webhook")
@app.get("/mp/webhook")
async def mp_webhook(request: Request):
    params = dict(request.query_params)
    payload = {}
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

    # Busca status real no MP
    try:
        mp_data = await mp_get_payment(payment_id)
        status = mp_data.get("status", "unknown")
    except Exception:
        return JSONResponse({"ok": True, "error": "failed_to_fetch_payment"}, status_code=200)

    # Atualiza DB e libera se aprovado
    row = await db_get_payment(str(payment_id))
    if not row:
        return JSONResponse({"ok": True, "unknown_payment": True})

    mp_id, telegram_id, email, old_status, created_at, approved_at, expires_at, plan = row
    plan = plan or "30d"

    if status != old_status:
        await db_update_status(str(payment_id), status)

    # Marca expiração apenas na primeira vez que vira approved
    if status == "approved" and old_status != "approved":
        await db_mark_approved(str(payment_id), plan)

    # Se aprovado, libera acesso (gera link)
    if status == "approved":
        await grant_access(int(telegram_id))

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
