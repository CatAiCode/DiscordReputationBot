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

HELP_TEXT = (
    "ℹ️ **Tip:**\n"
    "• Use `/rep` to give reputation\n"
    "• Use `/rate` to rate a user (1–5 ⭐)"
)

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
# LEADERBOARD EMBED
# ========================

async def make_leaderboard_embed(items, page, guild, bot, viewer_id):
    total_pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    embed = discord.Embed(
        title="🏆 Reputation Leaderboard",
        color=discord.Color.gold()
    )

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE

    for index, (uid, rep) in enumerate(items[start:end], start=start + 1):
        member = guild.get_member(uid)
        if not member:
            try:
                member = await bot.fetch_user(uid)
            except:
                member = None

        name = member.display_name if member else f"User ID {uid}"
        medal = RANK_EMOJIS.get(index, f"`#{index}`")

        avg, count = get_rating(uid)
        rating = (
            f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
            if avg else "☆☆☆☆☆ (No ratings)"
        )

        embed.add_field(
            name=f"{medal} {name}",
            value=f"{rating}\n🎖️ **{rep} reputation**",
            inline=False
        )

    # Your stats
    viewer_rank = None
    viewer_rep = 0
    for i, (uid, rep) in enumerate(items, start=1):
        if uid == viewer_id:
            viewer_rank = i
            viewer_rep = rep
            break

    avg, count = get_rating(viewer_id)
    rating = (
        f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
        if avg else "☆☆☆☆☆ (No ratings)"
    )

    embed.add_field(
        name="━━━━━━━━━━\n👤 Your Stats",
        value=(
            f"🏅 **Rank:** {f'#{viewer_rank}' if viewer_rank else 'Unranked'}\n"
            f"{rating}\n"
            f"🎖️ **{viewer_rep} reputation**"
        ),
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

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ This leaderboard isn’t yours.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, _):
        self.page -= 1
        self.update_buttons()
        embed = await make_leaderboard_embed(
            self.items, self.page, self.guild, self.bot, self.author_id
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, _):
        self.page += 1
        self.update_buttons()
        embed = await make_leaderboard_embed(
            self.items, self.page, self.guild, self.bot, self.author_id
        )
        await interaction.response.edit_message(embed=embed, view=self)

# ========================
# BOT
# ========================

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    # HARD RESET slash commands to fix "....."
    await bot.tree.clear_commands(guild=None)
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

# ========================
# COMMANDS (ALL DESCRIPTIONS SET)
# ========================

@bot.tree.command(name="rep", description="Give +1 reputation to a member")
@app_commands.checks.cooldown(1, 240)
async def rep(interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    new_val = add_rep(member.id, 1)
    avg, count = get_rating(member.id)

    rating = (
        f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
        if avg else "☆☆☆☆☆ (No ratings)"
    )

    await interaction.response.send_message(
        f"🎖️ {member.mention} now has **{new_val} reputation**\n"
        f"{rating}\n\n{HELP_TEXT}"
    )

@bot.tree.command(name="norep", description="Remove 1 reputation from a member")
@app_commands.checks.cooldown(1, 240)
async def norep(interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    new_val = add_rep(member.id, -1)
    avg, count = get_rating(member.id)

    rating = (
        f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
        if avg else "☆☆☆☆☆ (No ratings)"
    )

    await interaction.response.send_message(
        f"🎖️ {member.mention} now has **{new_val} reputation**\n"
        f"{rating}\n\n{HELP_TEXT}"
    )

@bot.tree.command(name="rate", description="Rate a member from 1 to 5 stars")
async def rate(interaction, member: discord.Member, stars: app_commands.Range[int, 1, 5]):
    if account_age_days(interaction.user) < MIN_RATING_ACCOUNT_AGE_DAYS:
        return await interaction.response.send_message("❌ Account too new.", ephemeral=True)

    set_rating(interaction.user.id, member.id, stars)
    avg, count = get_rating(member.id)

    await interaction.response.send_message(
        f"⭐ Rated {member.mention}\n"
        f"{render_rating_stars(avg)} ({avg}/5 • {count} votes)"
    )

@bot.tree.command(name="checkrep", description="Check a user's reputation and rating")
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
        f"👤 **{member.display_name}**\n"
        f"{rating}\n"
        f"🎖️ **{rep} reputation**"
    )

@bot.tree.command(name="leaderboard", description="View the reputation leaderboard")
async def leaderboard(interaction):
    items = get_sorted_rep_items()
    embed = await make_leaderboard_embed(
        items, 0, interaction.guild, bot, interaction.user.id
    )
    view = LeaderboardView(items, interaction.guild, bot, interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="importrep", description="Import reputation data from JSON")
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

@bot.tree.command(name="exportrep", description="Export reputation data to JSON")
async def exportrep(interaction):
    with get_db() as conn:
        rows = conn.execute("SELECT user_id, rep FROM reputation").fetchall()

    path = "/tmp/rep_export.json"
    with open(path, "w") as f:
        json.dump({str(uid): rep for uid, rep in rows}, f, indent=2)

    await interaction.response.send_message(
        "📦 Reputation export",
        file=discord.File(path),
        ephemeral=True
    )

# ========================
# RUN
# ========================

bot.run(TOKEN)
