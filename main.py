import json
import math
import sqlite3
from datetime import datetime, timezone, timedelta
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
DOWNREP_COOLDOWN_HOURS = 12

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

        conn.execute("""
        CREATE TABLE IF NOT EXISTS downreps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id INTEGER NOT NULL,
            actor_id INTEGER NOT NULL,
            created_at TEXT
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

        current = row[0] if row else 0
        new_val = max(0, current + amount)

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

def add_downrep(target_id: int, actor_id: int):
    with get_db() as conn:
        conn.execute("""
        INSERT INTO downreps (target_id, actor_id, created_at)
        VALUES (?, ?, ?)
        """, (target_id, actor_id, datetime.utcnow().isoformat()))
        conn.commit()

def get_downrep_count(user_id: int) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM downreps WHERE target_id = ?",
            (user_id,)
        ).fetchone()
    return row[0] if row else 0

def can_downrep(target_id: int, actor_id: int) -> tuple[bool, Optional[int]]:
    """
    Returns (allowed, minutes_remaining)
    """
    with get_db() as conn:
        row = conn.execute("""
            SELECT created_at
            FROM downreps
            WHERE target_id = ? AND actor_id = ?
            ORDER BY created_at DESC
            LIMIT 1
        """, (target_id, actor_id)).fetchone()

    if not row:
        return True, None

    last_time = datetime.fromisoformat(row[0])
    now = datetime.utcnow()
    delta = now - last_time

    cooldown = timedelta(hours=DOWNREP_COOLDOWN_HOURS)
    if delta >= cooldown:
        return True, None

    remaining = cooldown - delta
    minutes_left = math.ceil(remaining.total_seconds() / 60)
    return False, minutes_left

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
        return conn.execute("""
            SELECT user_id, rep
            FROM reputation
            ORDER BY rep DESC
        """).fetchall()

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

def calculate_trust_percentage(uprep: int, downrep: int) -> str:
    total = uprep + downrep
    if total == 0:
        return "N/A"
    return f"{round((uprep / total) * 100)}%"

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

@bot.tree.command(name="rep", description="Give 👍 reputation to a member")
@app_commands.checks.cooldown(1, 240)
async def rep(interaction: discord.Interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    uprep = add_rep(member.id, 1)
    downrep = get_downrep_count(member.id)
    trust = calculate_trust_percentage(uprep, downrep)
    avg, count = get_rating(member.id)

    rating = (
        f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
        if avg else "☆☆☆☆☆ (No ratings)"
    )

    await interaction.response.send_message(
        f"👍 **Reputation Given**\n\n"
        f"👤 **From:** {interaction.user.mention}\n"
        f"➡️ **To:** {member.mention}\n\n"
        f"👍 **UpRep:** {uprep}\n"
        f"👎 **DownRep:** {downrep}\n"
        f"📈 **Trust:** {trust}\n"
        f"{rating}"
    )

@bot.tree.command(name="downrep", description="Record 👎 feedback (12h cooldown per user)")
@app_commands.checks.cooldown(1, 240)
async def downrep(interaction: discord.Interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    allowed, minutes_left = can_downrep(member.id, interaction.user.id)
    if not allowed:
        return await interaction.response.send_message(
            f"⏳ You can downrep this user again in **{minutes_left} minutes**.",
            ephemeral=True
        )

    add_downrep(member.id, interaction.user.id)

    uprep = get_rep(member.id)
    downrep_count = get_downrep_count(member.id)
    trust = calculate_trust_percentage(uprep, downrep_count)
    avg, count = get_rating(member.id)

    rating = (
        f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
        if avg else "☆☆☆☆☆ (No ratings)"
    )

    await interaction.response.send_message(
        f"👎 **Feedback Recorded**\n\n"
        f"👤 **From:** {interaction.user.mention}\n"
        f"➡️ **To:** {member.mention}\n\n"
        f"👍 **UpRep:** {uprep}\n"
        f"👎 **DownRep:** {downrep_count}\n"
        f"📉 **Trust:** {trust}\n"
        f"{rating}"
    )

@bot.tree.command(name="checkrep", description="Check reputation and rating")
async def checkrep(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user

    uprep = get_rep(member.id)
    downrep = get_downrep_count(member.id)
    trust = calculate_trust_percentage(uprep, downrep)
    avg, count = get_rating(member.id)

    rating = (
        f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
        if avg else "☆☆☆☆☆ (No ratings)"
    )

    await interaction.response.send_message(
        f"📊 **Reputation Overview**\n\n"
        f"👤 **{member.display_name}**\n\n"
        f"👍 **UpRep:** {uprep}\n"
        f"👎 **DownRep:** {downrep}\n"
        f"📈 **Trust:** {trust}\n"
        f"{rating}"
    )

# ========================
# RUN
# ========================

bot.run(TOKEN)
