import json
import math
import sqlite3
from datetime import datetime
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

REP_PER_LEVEL = 20  # 20 rep = 1 level (uncapped)

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
        new_val = max(0, new_val)  # prevent negative rep

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

# ========================
# UTILITIES
# ========================

def get_trading_level(rep: int) -> int:
    return rep // REP_PER_LEVEL  # uncapped

def compact_stats(rep: int):
    level = get_trading_level(rep)
    return f"🏅 **{rep} Rep** • 🔰 **Lv. {level}**"

# ========================
# LEADERBOARD EMBED
# ========================

async def make_leaderboard_embed(items, page, guild, bot, viewer_id: int):
    total_pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    embed = discord.Embed(
        title="🏆 Reputation Leaderboard",
        color=discord.Color.gold()
    )

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE

    for index, (user_id, rep_amount) in enumerate(items[start:end], start=start + 1):
        member = guild.get_member(user_id)
        if not member:
            try:
                member = await bot.fetch_user(user_id)
            except:
                member = None

        name = member.display_name if member else f"User ID {user_id}"
        medal = RANK_EMOJIS.get(index, f"`#{index}`")
        level = get_trading_level(rep_amount)

        embed.add_field(
            name=f"{medal} {name}",
            value=f"🏅 **{rep_amount} Rep** • 🔰 **Lv. {level}**",
            inline=False
        )

    viewer_rep = get_rep(viewer_id)
    viewer_level = get_trading_level(viewer_rep)

    embed.add_field(
        name="━━━━━━━━━━\n👤 Your Stats",
        value=f"🏅 **{viewer_rep} Rep** • 🔰 **Lv. {viewer_level}**",
        inline=False
    )

    embed.set_footer(text=f"Page {page + 1}/{total_pages}")
    return embed

# ========================
# PAGINATION VIEW
# ========================

class LeaderboardView(discord.ui.View):
    def __init__(self, items, guild, bot, author_id):
        super().__init__(timeout=120)
        self.items = items
        self.guild = guild
        self.bot = bot
        self.author_id = author_id
        self.page = 0
        self.max_pages = max(1, math.ceil(len(items) / PAGE_SIZE))
        self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.max_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ You can’t control someone else’s leaderboard.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page -= 1
        self.update_buttons()
        embed = await make_leaderboard_embed(
            self.items, self.page, self.guild, self.bot, self.author_id
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page += 1
        self.update_buttons()
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
# COMMANDS
# ========================

@bot.tree.command(name="rep", description="Give +1 reputation to a member")
@app_commands.checks.cooldown(1, 240)
async def rep(interaction: discord.Interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    new_val = add_rep(member.id, 1)

    await interaction.response.send_message(
        f"🎖️ {interaction.user.mention} → {member.mention} **(+1 Rep)**\n"
        f"{compact_stats(new_val)}"
    )

@bot.tree.command(name="norep", description="Give -1 reputation to a member")
@app_commands.checks.cooldown(1, 240)
async def norep(interaction: discord.Interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    new_val = add_rep(member.id, -1)

    await interaction.response.send_message(
        f"⚠️ {interaction.user.mention} → {member.mention} **(-1 Rep)**\n"
        f"{compact_stats(new_val)}"
    )

@bot.tree.command(name="checkrep", description="Check reputation and trading level")
async def checkrep(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    rep = get_rep(member.id)

    await interaction.response.send_message(
        f"📊 {member.mention}\n"
        f"{compact_stats(rep)}"
    )

@bot.tree.command(name="leaderboard", description="View the reputation leaderboard")
async def leaderboard(interaction: discord.Interaction):
    items = get_sorted_rep_items()
    if not items:
        return await interaction.response.send_message(
            "📭 No reputation data yet.", ephemeral=True
        )

    embed = await make_leaderboard_embed(
        items, 0, interaction.guild, bot, interaction.user.id
    )
    view = LeaderboardView(items, interaction.guild, bot, interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="importrep", description="Import reputation from JSON")
async def importrep(interaction: discord.Interaction, file: discord.Attachment):
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
async def exportrep(interaction: discord.Interaction):
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
