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

# ------------------------
# CONFIG
# ------------------------

MIN_ACCOUNT_AGE_DAYS = 7
PAGE_SIZE = 10
DB_PATH = "/data/reputation.db"

RANK_EMOJIS = {
    1: "🥇",
    2: "🥈",
    3: "🥉"
}

# ------------------------
# LOAD TOKEN
# ------------------------

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN missing")

# ------------------------
# SQLITE SETUP
# ------------------------

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
        conn.commit()

init_db()

# ------------------------
# DB HELPERS
# ------------------------

def set_rep(user_id: int, amount: int) -> int:
    with get_db() as conn:
        conn.execute("""
        INSERT INTO reputation (user_id, rep, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET rep = excluded.rep,
                      updated_at = excluded.updated_at
        """, (user_id, amount, datetime.utcnow().isoformat()))
        conn.commit()
    return amount

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

def get_sorted_rep_items():
    with get_db() as conn:
        return conn.execute(
            "SELECT user_id, rep FROM reputation ORDER BY rep DESC"
        ).fetchall()

# ------------------------
# ACCOUNT AGE CHECK
# ------------------------

def meets_account_age_requirement(user: discord.abc.User) -> bool:
    account_age = datetime.now(timezone.utc) - user.created_at
    return account_age >= timedelta(days=MIN_ACCOUNT_AGE_DAYS)

# ------------------------
# FANCY LEADERBOARD
# ------------------------

async def make_leaderboard_embed(sorted_items, page, guild, bot):
    total_entries = len(sorted_items)
    total_pages = max(1, math.ceil(total_entries / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = sorted_items[start:end]

    embed = discord.Embed(
        title="🏆 Reputation Leaderboard",
        color=discord.Color.gold()
    )

    for index, (user_id, rep_amount) in enumerate(page_items, start=start + 1):
        member = guild.get_member(user_id)
        if not member:
            try:
                member = await bot.fetch_user(user_id)
            except:
                member = None

        name = member.name if member else f"User ID {user_id}"
        medal = RANK_EMOJIS.get(index, f"`#{index}`")

        embed.add_field(
            name=f"{medal} {name}",
            value=f"⭐ **{rep_amount} rep**",
            inline=False
        )

    embed.set_footer(
        text=f"Page {page + 1}/{total_pages} • Total users: {total_entries}"
    )

    if page == 0 and page_items:
        top_user_id = page_items[0][0]
        top_member = guild.get_member(top_user_id)
        if not top_member:
            try:
                top_member = await bot.fetch_user(top_user_id)
            except:
                top_member = None

        if top_member:
            embed.set_thumbnail(url=top_member.display_avatar.url)

    return embed

class LeaderboardView(discord.ui.View):
    def __init__(self, sorted_items, guild, author_id, bot):
        super().__init__(timeout=120)
        self.sorted_items = sorted_items
        self.guild = guild
        self.author_id = author_id
        self.bot = bot
        self.page = 0
        self.update_buttons()

    def update_buttons(self):
        total_pages = max(1, math.ceil(len(self.sorted_items) / PAGE_SIZE))
        self.children[0].disabled = self.page <= 0
        self.children[1].disabled = self.page >= total_pages - 1

    async def update(self, interaction):
        self.update_buttons()
        embed = await make_leaderboard_embed(
            self.sorted_items, self.page, interaction.guild, self.bot
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⬅ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "Not your leaderboard.", ephemeral=True
            )
        self.page -= 1
        await self.update(interaction)

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "Not your leaderboard.", ephemeral=True
            )
        self.page += 1
        await self.update(interaction)

# ------------------------
# BOT SETUP
# ------------------------

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await bot.tree.sync()

# ------------------------
# COMMANDS
# ------------------------

@bot.tree.command(name="leaderboard")
async def leaderboard(interaction):
    items = get_sorted_rep_items()
    if not items:
        return await interaction.response.send_message(
            "📭 No rep data yet.", ephemeral=True
        )

    embed = await make_leaderboard_embed(items, 0, interaction.guild, bot)
    view = LeaderboardView(items, interaction.guild, interaction.user.id, bot)
    await interaction.response.send_message(embed=embed, view=view)

# ------------------------
# RUN
# ------------------------

bot.run(TOKEN)
