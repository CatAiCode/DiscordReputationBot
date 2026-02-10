import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Optional
import os

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# ========================
# CONFIG
# ========================

MIN_RATING_ACCOUNT_AGE_DAYS = 10
PAGE_SIZE = 10
DB_PATH = "/data/reputation.db"
MAX_RATING = 5

RANK_EMOJIS = {
    1: "🥇",
    2: "🥈",
    3: "🥉"
}

# ========================
# LOAD TOKEN
# ========================

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN missing")

# ========================
# SQLITE SETUP
# ========================

def get_db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with get_db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS reputation (
            user_id INTEGER PRIMARY KEY,
            rep INTEGER NOT NULL,
            updated_at TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            rater_id INTEGER,
            target_id INTEGER,
            rating INTEGER,
            rated_at TEXT,
            PRIMARY KEY (rater_id, target_id)
        )
        """)
        conn.commit()

init_db()

# ========================
# DATABASE HELPERS
# ========================

def add_rep(user_id: int, amount: int) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT rep FROM reputation WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        new_val = (row[0] if row else 0) + amount

        conn.execute("""
        INSERT INTO reputation (user_id, rep, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET rep = excluded.rep,
                      updated_at = excluded.updated_at
        """, (user_id, new_val, datetime.utcnow().isoformat()))
        conn.commit()

    return new_val

def get_rep(user_id: int) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT rep FROM reputation WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    return row[0] if row else 0

def set_rating(rater_id: int, target_id: int, rating: int):
    with get_db() as conn:
        conn.execute("""
        INSERT INTO ratings (rater_id, target_id, rating, rated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(rater_id, target_id)
        DO UPDATE SET rating = excluded.rating,
                      rated_at = excluded.rated_at
        """, (rater_id, target_id, rating, datetime.utcnow().isoformat()))
        conn.commit()

def get_rating(target_id: int):
    with get_db() as conn:
        avg, count = conn.execute("""
            SELECT AVG(rating), COUNT(*)
            FROM ratings
            WHERE target_id = ?
        """, (target_id,)).fetchone()
    return (round(avg, 2), count) if count else (None, 0)

def get_sorted_rep_items():
    with get_db() as conn:
        return conn.execute(
            "SELECT user_id, rep FROM reputation ORDER BY rep DESC"
        ).fetchall()

# ========================
# UTILITIES
# ========================

def account_age_days(user: discord.abc.User) -> int:
    return (datetime.now(timezone.utc) - user.created_at).days

def render_rating_stars(avg: float) -> str:
    rounded = round(avg * 2) / 2
    full = int(rounded)
    empty = MAX_RATING - full
    return "⭐" * full + "☆" * empty

# ========================
# BOT SETUP
# ========================

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

# ========================
# COMMANDS
# ========================

@bot.tree.command(name="rep", description="Give +1 reputation to a member")
@app_commands.checks.cooldown(1, 240)
async def rep(interaction: discord.Interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    new_val = add_rep(member.id, 1)
    avg, count = get_rating(member.id)

    rating_text = (
        f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
        if avg else "☆☆☆☆☆ (No ratings)"
    )

    await interaction.response.send_message(
        f"🎖️ **Reputation Given**\n\n"
        f"👤 **From:** {interaction.user.mention}\n"
        f"➡️ **To:** {member.mention}\n\n"
        f"🎖️ **New Reputation:** {new_val}\n"
        f"{rating_text}"
    )

@bot.tree.command(name="norep", description="Give -1 reputation to a member")
@app_commands.checks.cooldown(1, 240)
async def norep(interaction: discord.Interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    new_val = add_rep(member.id, -1)
    avg, count = get_rating(member.id)

    rating_text = (
        f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
        if avg else "☆☆☆☆☆ (No ratings)"
    )

    await interaction.response.send_message(
        f"⚠️ **Reputation Removed**\n\n"
        f"👤 **From:** {interaction.user.mention}\n"
        f"➡️ **To:** {member.mention}\n\n"
        f"🎖️ **New Reputation:** {new_val}\n"
        f"{rating_text}"
    )

@bot.tree.command(name="rate", description="Rate a member from 1 to 5 stars")
async def rate(
    interaction: discord.Interaction,
    member: discord.Member,
    stars: app_commands.Range[int, 1, 5]
):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    if account_age_days(interaction.user) < MIN_RATING_ACCOUNT_AGE_DAYS:
        return await interaction.response.send_message("❌ Account too new to rate.", ephemeral=True)

    set_rating(interaction.user.id, member.id, stars)
    avg, count = get_rating(member.id)

    await interaction.response.send_message(
        f"⭐ **Rating Submitted**\n\n"
        f"👤 **From:** {interaction.user.mention}\n"
        f"➡️ **To:** {member.mention}\n\n"
        f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
    )

@bot.tree.command(name="exportrep", description="Export all reputation data to JSON")
@app_commands.checks.has_permissions(administrator=True)
async def exportrep(interaction: discord.Interaction):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id, rep FROM reputation"
        ).fetchall()

    data = {str(user_id): rep for user_id, rep in rows}

    path = "/tmp/reputation_export.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    await interaction.response.send_message(
        "📦 **Reputation export complete**",
        file=discord.File(path),
        ephemeral=True
    )

# ========================
# RUN
# ========================

bot.run(TOKEN)
