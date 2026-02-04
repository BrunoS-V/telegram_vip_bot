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

# pode deixar vazio por enquanto e pegar via /get_channel_id
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Acesso Canal VIP").strip()
PRICE = float(os.getenv("PRICE", "29.90"))
CURRENCY = os.getenv("CURRENCY", "BRL").strip()

# duração da assinatura (em dias)
SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))

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
