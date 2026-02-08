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

# =========================
# CONFIG
# =========================

MIN_ACCOUNT_AGE_DAYS = 7
PAGE_SIZE = 10
DB_PATH = "/data/reputation.db"

# =========================
# ENV
# =========================

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN missing")

# =========================
# SQLITE SETUP
# =========================

def get_db():
    return sqlite3.connect(DB_PATH)

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

# =========================
# DB HELPERS
# =========================

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

def get_sorted_rep_items(mode: str):
    query = "SELECT user_id, rep FROM reputation"
    params = ()

    if mode == "weekly":
        query += " WHERE updated_at >= ?"
        params = ((datetime.utcnow() - timedelta(days=7)).isoformat(),)
    elif mode == "monthly":
        query += " WHERE updated_at >= ?"
        params = ((datetime.utcnow() - timedelta(days=30)).isoformat(),)

    query += " ORDER BY rep DESC"

    with get_db() as conn:
        return conn.execute(query, params).fetchall()

# =========================
# HELPERS
# =========================

def meets_account_age_requirement(user: discord.abc.User) -> bool:
    return (datetime.now(timezone.utc) - user.created_at) >= timedelta(days=MIN_ACCOUNT_AGE_DAYS)

def rank_emoji(rank: int) -> str:
    if rank == 1:
        return "🥇 👑"
    if rank == 2:
        return "🥈"
    if rank == 3:
        return "🥉"
    return f"#{rank}"

# =========================
# LEADERBOARD EMBED
# =========================

def make_leaderboard_embed(items, page, guild, viewer_id, mode):
    total_entries = len(items)
    total_pages = max(1, math.ceil(total_entries / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = items[start:end]

    titles = {
        "all": "🏆 Reputation Leaderboard — All Time",
        "weekly": "🗓 Reputation Leaderboard — Weekly",
        "monthly": "📅 Reputation Leaderboard — Monthly",
    }

    embed = discord.Embed(
        title=titles[mode],
        color=discord.Color.gold()
    )

    viewer_rank = None
    viewer_rep = None

    for idx, (uid, rep) in enumerate(items, start=1):
        if uid == viewer_id:
            viewer_rank = idx
            viewer_rep = rep
            break

    for idx, (uid, rep) in enumerate(page_items, start=start + 1):
        member = guild.get_member(uid)
        name = member.mention if member else f"<@{uid}>"

        embed.add_field(
            name=f"{rank_emoji(idx)} {name}",
            value=f"⭐ **{rep}** reputation",
            inline=False
        )

    if viewer_rank:
        embed.add_field(
            name="━━━━━━━━━━━",
            value=f"👤 **Your rank:** {rank_emoji(viewer_rank)} • ⭐ **{viewer_rep}** rep",
            inline=False
        )

    embed.set_footer(text=f"Page {page + 1}/{total_pages} • Total users: {total_entries}")
    return embed

# =========================
# LEADERBOARD VIEW (6 BUTTONS)
# =========================

class LeaderboardView(discord.ui.View):
    def __init__(self, guild, author_id):
        super().__init__(timeout=180)
        self.guild = guild
        self.author_id = author_id

        self.mode = "all"
        self.pages = {"all": 0, "weekly": 0, "monthly": 0}
        self.data = {
            "all": get_sorted_rep_items("all"),
            "weekly": get_sorted_rep_items("weekly"),
            "monthly": get_sorted_rep_items("monthly"),
        }

    async def refresh(self, interaction):
        embed = make_leaderboard_embed(
            self.data[self.mode],
            self.pages[self.mode],
            self.guild,
            interaction.user.id,
            self.mode
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⬅", style=discord.ButtonStyle.primary, row=0)
    async def prev_page(self, interaction, button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your leaderboard.", ephemeral=True)
        if self.pages[self.mode] > 0:
            self.pages[self.mode] -= 1
        await self.refresh(interaction)

    @discord.ui.button(label="➡", style=discord.ButtonStyle.primary, row=0)
    async def next_page(self, interaction, button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your leaderboard.", ephemeral=True)
        max_pages = max(1, math.ceil(len(self.data[self.mode]) / PAGE_SIZE))
        if self.pages[self.mode] < max_pages - 1:
            self.pages[self.mode] += 1
        await self.refresh(interaction)

    @discord.ui.button(label="🗓 Weekly", style=discord.ButtonStyle.secondary, row=1)
    async def weekly(self, interaction, button):
        self.mode = "weekly"
        await self.refresh(interaction)

    @discord.ui.button(label="📅 Monthly", style=discord.ButtonStyle.secondary, row=1)
    async def monthly(self, interaction, button):
        self.mode = "monthly"
        await self.refresh(interaction)

    @discord.ui.button(label="🏆 All-Time", style=discord.ButtonStyle.secondary, row=1)
    async def all_time(self, interaction, button):
        self.mode = "all"
        await self.refresh(interaction)

# =========================
# BOT SETUP
# =========================

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await bot.tree.sync()

# =========================
# COMMANDS
# =========================

@bot.tree.command(name="rep")
@app_commands.checks.cooldown(1, 240)
async def rep(interaction, member: discord.Member):
    user = interaction.user
    if not meets_account_age_requirement(user):
        return await interaction.response.send_message("❌ Account too new.", ephemeral=True)
    if member.id == user.id or member.bot:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    new_val = add_rep(member.id, 1)
    await interaction.response.send_message(
        f"👍 {user.mention} gave **+1 rep** to {member.mention}!\n⭐ New rep: **{new_val}**"
    )

@rep.error
async def rep_error(interaction, error):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏳ Try again in **{int(error.retry_after)}s**.",
            ephemeral=True
        )

@bot.tree.command(name="norep")
@app_commands.checks.cooldown(1, 240)
async def norep(interaction, member: discord.Member):
    user = interaction.user
    if not meets_account_age_requirement(user):
        return await interaction.response.send_message("❌ Account too new.", ephemeral=True)
    if member.id == user.id or member.bot:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    new_val = add_rep(member.id, -1)
    await interaction.response.send_message(
        f"⚠️ {user.mention} gave **-1 rep** to {member.mention}.\n⭐ New rep: **{new_val}**"
    )

@bot.tree.command(name="setrep")
async def setrep(interaction, member: discord.Member, amount: int):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)
    if not -1000 <= amount <= 1000:
        return await interaction.response.send_message("⚠️ Amount must be between -1000 and 1000.", ephemeral=True)

    set_rep(member.id, amount)
    await interaction.response.send_message(
        f"🛠️ Set {member.mention}'s rep to **{amount}**."
    )

@bot.tree.command(name="checkrep")
async def checkrep(interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    await interaction.response.send_message(
        f"📊 {member.mention} has **{get_rep(member.id)}** rep."
    )

@bot.tree.command(name="leaderboard")
async def leaderboard(interaction):
    view = LeaderboardView(interaction.guild, interaction.user.id)
    embed = make_leaderboard_embed(
        view.data["all"],
        0,
        interaction.guild,
        interaction.user.id,
        "all"
    )
    await interaction.response.send_message(embed=embed, view=view)

# =========================
# IMPORT / EXPORT
# =========================

@bot.tree.command(name="importrep")
async def importrep(interaction, file: discord.Attachment):
    data = json.loads(await file.read())
    with get_db() as conn:
        for uid, rep in data.items():
            try:
                uid = int(uid)
                rep = int(rep)
            except Exception:
                continue
            conn.execute("""
            INSERT INTO reputation (user_id, rep, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET rep = excluded.rep,
                          updated_at = excluded.updated_at
            """, (uid, rep, datetime.utcnow().isoformat()))
        conn.commit()
    await interaction.response.send_message("✅ Reputation imported.")

@bot.tree.command(name="exportrep")
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

# =========================
# RUN
# =========================

bot.run(TOKEN)
