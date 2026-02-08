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

MIN_ACCOUNT_AGE_DAYS = 7
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
# DB HELPERS
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

def set_rep(user_id: int, amount: int):
    with get_db() as conn:
        conn.execute("""
        INSERT INTO reputation (user_id, rep, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET rep = excluded.rep,
                      updated_at = excluded.updated_at
        """, (user_id, amount, datetime.utcnow().isoformat()))
        conn.commit()

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
# UTIL
# ========================

def account_age_days(user: discord.abc.User) -> int:
    return (datetime.now(timezone.utc) - user.created_at).days

def render_rating_stars(avg: float) -> str:
    rounded = round(avg * 2) / 2
    full = int(rounded)
    empty = MAX_RATING - full
    return "⭐" * full + "☆" * empty

# ========================
# LEADERBOARD
# ========================

async def make_leaderboard_embed(sorted_items, page, guild, bot):
    total_pages = max(1, math.ceil(len(sorted_items) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    embed = discord.Embed(
        title="🏆 Reputation Leaderboard",
        color=discord.Color.gold()
    )

    start = page * PAGE_SIZE
    for index, (user_id, rep_amount) in enumerate(
        sorted_items[start:start + PAGE_SIZE], start=start + 1
    ):
        member = guild.get_member(user_id)
        if not member:
            try:
                member = await bot.fetch_user(user_id)
            except:
                member = None

        name = member.name if member else f"User ID {user_id}"
        medal = RANK_EMOJIS.get(index, f"`#{index}`")

        avg, count = get_rating(user_id)
        rating_line = (
            f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
            if avg else "☆☆☆☆☆ (No ratings)"
        )

        embed.add_field(
            name=f"{medal} {name}",
            value=f"{rating_line}\n⭐ **{rep_amount} reputation**",
            inline=False
        )

    embed.set_footer(text=f"Page {page + 1}/{total_pages}")
    return embed

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
async def rep(interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message(
            "❌ Invalid target.", ephemeral=True
        )

    new_val = add_rep(member.id, 1)
    await interaction.response.send_message(
        f"⭐ {member.mention} now has **{new_val} reputation**"
    )

@bot.tree.command(name="norep", description="Give -1 reputation to a member")
@app_commands.checks.cooldown(1, 240)
async def norep(interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message(
            "❌ Invalid target.", ephemeral=True
        )

    new_val = add_rep(member.id, -1)
    await interaction.response.send_message(
        f"⭐ {member.mention} now has **{new_val} reputation**"
    )

@bot.tree.command(name="rate", description="Rate a member from 1 to 5 stars")
async def rate(interaction, member: discord.Member, stars: app_commands.Range[int, 1, 5]):
    if account_age_days(interaction.user) < MIN_RATING_ACCOUNT_AGE_DAYS:
        return await interaction.response.send_message(
            "❌ Account too new to rate.", ephemeral=True
        )

    set_rating(interaction.user.id, member.id, stars)
    avg, count = get_rating(member.id)

    await interaction.response.send_message(
        f"⭐ Rated {member.mention}\n"
        f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
    )

@bot.tree.command(name="checkrep", description="Check reputation and rating")
async def checkrep(interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    rep = get_rep(member.id)
    avg, count = get_rating(member.id)

    rating = (
        f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
        if avg else "☆☆☆☆☆ (No ratings)"
    )

    await interaction.response.send_message(
        f"📊 **Reputation & Rating Check**\n\n"
        f"👤 {member.mention}\n"
        f"{rating}\n"
        f"⭐ **{rep} reputation**"
    )

@bot.tree.command(name="leaderboard", description="View the reputation leaderboard")
async def leaderboard(interaction):
    items = get_sorted_rep_items()
    embed = await make_leaderboard_embed(items, 0, interaction.guild, bot)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="importrep", description="Import reputation from JSON")
async def importrep(interaction, file: discord.Attachment):
    data = json.loads(await file.read())
    with get_db() as conn:
        for uid, rep in data.items():
            conn.execute("""
            INSERT INTO reputation (user_id, rep, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET rep = excluded.rep,
                          updated_at = excluded.updated_at
            """, (int(uid), int(rep), datetime.utcnow().isoformat()))
        conn.commit()

    await interaction.response.send_message("✅ Reputation imported.")

@bot.tree.command(name="exportrep", description="Export reputation to JSON")
async def exportrep(interaction):
    with get_db() as conn:
        rows = conn.execute("SELECT user_id, rep FROM reputation").fetchall()

    path = "/tmp/rep_export.json"
    with open(path, "w") as f:
        json.dump({str(uid): rep for uid, rep in rows}, f, indent=2)

    await interaction.response.send_message(
        "📦 Reputation export:",
        file=discord.File(path),
        ephemeral=True
    )

# ========================
# RUN
# ========================

bot.run(TOKEN)
