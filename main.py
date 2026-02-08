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
# FANCY LEADERBOARD (NO AVATAR)
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
# COMMANDS (DESCRIPTIONS FIXED)
# ------------------------

@bot.tree.command(
    name="rep",
    description="Give +1 reputation to a member"
)
@app_commands.checks.cooldown(1, 240)
async def rep(interaction, member: discord.Member):
    user = interaction.user

    if not meets_account_age_requirement(user):
        return await interaction.response.send_message(
            "❌ Account too new.", ephemeral=True
        )
    if member.id == user.id or member.bot:
        return await interaction.response.send_message(
            "❌ Invalid target.", ephemeral=True
        )

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

@bot.tree.command(
    name="norep",
    description="Give -1 reputation to a member"
)
@app_commands.checks.cooldown(1, 240)
async def norep(interaction, member: discord.Member):
    user = interaction.user

    if not meets_account_age_requirement(user):
        return await interaction.response.send_message(
            "❌ Account too new.", ephemeral=True
        )
    if member.id == user.id or member.bot:
        return await interaction.response.send_message(
            "❌ Invalid target.", ephemeral=True
        )

    new_val = add_rep(member.id, -1)
    await interaction.response.send_message(
        f"⚠️ {user.mention} gave **-1 rep** to {member.mention}.\n⭐ New rep: **{new_val}**"
    )

@bot.tree.command(
    name="setrep",
    description="Set a member's reputation to a specific value"
)
async def setrep(interaction, member: discord.Member, amount: int):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message(
            "❌ Invalid target.", ephemeral=True
        )
    if not -1000 <= amount <= 1000:
        return await interaction.response.send_message(
            "⚠️ Amount must be between -1000 and 1000.",
            ephemeral=True
        )

    set_rep(member.id, amount)
    await interaction.response.send_message(
        f"🛠️ Set {member.mention}'s rep to **{amount}**."
    )

@bot.tree.command(
    name="checkrep",
    description="Check your own or another member's reputation"
)
async def checkrep(interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    await interaction.response.send_message(
        f"📊 {member.mention} has **{get_rep(member.id)}** rep."
    )

@bot.tree.command(
    name="leaderboard",
    description="View the reputation leaderboard"
)
async def leaderboard(interaction):
    items = get_sorted_rep_items()
    if not items:
        return await interaction.response.send_message(
            "📭 No rep data yet.", ephemeral=True
        )

    embed = await make_leaderboard_embed(items, 0, interaction.guild, bot)
    view = LeaderboardView(items, interaction.guild, interaction.user.id, bot)
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(
    name="importrep",
    description="Import reputation data from a JSON file"
)
async def importrep(interaction, file: discord.Attachment):
    content = await file.read()
    data = json.loads(content)

    inserted = 0
    with get_db() as conn:
        for uid, rep in data.items():
            try:
                uid = int(uid)
                rep = int(rep)
            except:
                continue

            conn.execute("""
            INSERT INTO reputation (user_id, rep, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET rep = excluded.rep,
                          updated_at = excluded.updated_at
            """, (uid, rep, datetime.utcnow().isoformat()))
            inserted += 1
        conn.commit()

    await interaction.response.send_message(
        f"✅ Imported **{inserted}** reputation entries."
    )

@bot.tree.command(
    name="exportrep",
    description="Export the reputation database as a JSON file"
)
async def exportrep(interaction):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id, rep FROM reputation"
        ).fetchall()

    data = {str(uid): rep for uid, rep in rows}
    path = "/tmp/rep_export.json"

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    await interaction.response.send_message(
        "📦 Reputation export:",
        file=discord.File(path),
        ephemeral=True
    )

# ------------------------
# RUN
# ------------------------

bot.run(TOKEN)
