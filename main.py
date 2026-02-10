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

PAGE_SIZE = 10
DB_PATH = "/data/reputation.db"
REP_PER_LEVEL = 20  # uncapped

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
# SQLITE SETUP + SAFE MIGRATION
# ========================

def get_db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def column_exists(conn, table, column):
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(col[1] == column for col in cols)

def init_db():
    with get_db() as conn:
        # Base table
        conn.execute("""
        CREATE TABLE IF NOT EXISTS reputation (
            user_id INTEGER PRIMARY KEY,
            rep INTEGER DEFAULT 0,
            updated_at TEXT
        )
        """)

        # Add columns safely
        if not column_exists(conn, "reputation", "rep_up"):
            conn.execute("ALTER TABLE reputation ADD COLUMN rep_up INTEGER DEFAULT 0")
        if not column_exists(conn, "reputation", "rep_down"):
            conn.execute("ALTER TABLE reputation ADD COLUMN rep_down INTEGER DEFAULT 0")

        # Migrate old rep → rep_up (one-time safe)
        conn.execute("""
        UPDATE reputation
        SET rep_up = rep
        WHERE rep_up = 0 AND rep > 0
        """)

        conn.commit()

init_db()

# ========================
# DATABASE HELPERS
# ========================

def add_rep_up(user_id: int):
    with get_db() as conn:
        conn.execute("""
        INSERT INTO reputation (user_id, rep_up, rep_down, updated_at)
        VALUES (?, 1, 0, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            rep_up = rep_up + 1,
            updated_at = excluded.updated_at
        """, (user_id, datetime.utcnow().isoformat()))
        conn.commit()

def add_rep_down(user_id: int):
    with get_db() as conn:
        conn.execute("""
        INSERT INTO reputation (user_id, rep_up, rep_down, updated_at)
        VALUES (?, 0, 1, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            rep_down = rep_down + 1,
            updated_at = excluded.updated_at
        """, (user_id, datetime.utcnow().isoformat()))
        conn.commit()

def get_rep(user_id: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT rep_up, rep_down FROM reputation WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    return row if row else (0, 0)

def get_sorted_rep_items():
    with get_db() as conn:
        return conn.execute("""
        SELECT user_id, rep_up, rep_down
        FROM reputation
        ORDER BY rep_up DESC
        """).fetchall()

# ========================
# UTILITIES
# ========================

def get_trading_level(rep_up: int) -> int:
    return rep_up // REP_PER_LEVEL

def compact_stats(rep_up: int, rep_down: int):
    return f"👍 **{rep_up}** • 👎 **{rep_down}** • 🔰 **Lv. {get_trading_level(rep_up)}**"

# ========================
# LEADERBOARD EMBED (PAGINATED)
# ========================

async def make_leaderboard_embed(items, page, guild, bot, viewer_id):
    total_pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    embed = discord.Embed(title="🏆 Reputation Leaderboard", color=discord.Color.gold())

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE

    for idx, (uid, up, down) in enumerate(items[start:end], start=start + 1):
        member = guild.get_member(uid) or await bot.fetch_user(uid)
        name = member.display_name if hasattr(member, "display_name") else member.name
        medal = RANK_EMOJIS.get(idx, f"`#{idx}`")

        embed.add_field(
            name=f"{medal} {name}",
            value=compact_stats(up, down),
            inline=False
        )

    viewer_up, viewer_down = get_rep(viewer_id)
    embed.add_field(
        name="━━━━━━━━━━\n👤 Your Stats",
        value=compact_stats(viewer_up, viewer_down),
        inline=False
    )

    embed.set_footer(text=f"Page {page + 1}/{total_pages}")
    return embed

# ========================
# PAGINATION VIEW (UNCHANGED)
# ========================

class LeaderboardView(discord.ui.View):
    def __init__(self, items, guild, bot, author_id):
        super().__init__(timeout=120)
        self.items = items
        self.guild = guild
        self.bot = bot
        self.author_id = author_id
        self.page = 0

    async def interaction_check(self, interaction):
        return interaction.user.id == self.author_id

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page -= 1
        embed = await make_leaderboard_embed(
            self.items, self.page, self.guild, self.bot, self.author_id
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page += 1
        embed = await make_leaderboard_embed(
            self.items, self.page, self.guild, self.bot, self.author_id
        )
        await interaction.response.edit_message(embed=embed, view=self)

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
# COMMANDS (UNCHANGED NAMES)
# ========================

@bot.tree.command(name="rep")
@app_commands.checks.cooldown(1, 240)
async def rep(interaction: discord.Interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    add_rep_up(member.id)
    up, down = get_rep(member.id)

    await interaction.response.send_message(
        f"👍 {interaction.user.mention} → {member.mention}\n"
        f"{compact_stats(up, down)}"
    )

@bot.tree.command(name="norep")
@app_commands.checks.cooldown(1, 240)
async def norep(interaction: discord.Interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    add_rep_down(member.id)
    up, down = get_rep(member.id)

    await interaction.response.send_message(
        f"👎 {interaction.user.mention} → {member.mention}\n"
        f"{compact_stats(up, down)}"
    )

@bot.tree.command(name="checkrep")
async def checkrep(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    up, down = get_rep(member.id)

    await interaction.response.send_message(
        f"📊 {member.mention}\n"
        f"{compact_stats(up, down)}"
    )

@bot.tree.command(name="leaderboard")
async def leaderboard(interaction: discord.Interaction):
    items = get_sorted_rep_items()
    embed = await make_leaderboard_embed(items, 0, interaction.guild, bot, interaction.user.id)
    view = LeaderboardView(items, interaction.guild, bot, interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="exportrep")
async def exportrep(interaction: discord.Interaction):
    with get_db() as conn:
        rows = conn.execute("SELECT user_id, rep_up, rep_down FROM reputation").fetchall()

    path = "/tmp/rep_export.json"
    with open(path, "w") as f:
        json.dump(
            {str(uid): {"up": up, "down": down} for uid, up, down in rows},
            f,
            indent=2
        )

    await interaction.response.send_message(
        "📦 Reputation export:",
        file=discord.File(path),
        ephemeral=True
    )

# ========================
# RUN
# ========================

bot.run(TOKEN)
