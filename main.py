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

REP_PER_LEVEL = 20  # uncapped: level = rep // 20

RANK_EMOJIS = {
    1: "🥇",
    2: "🥈",
    3: "🥉",
}

# ========================
# LOAD TOKEN
# ========================

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN missing")

# ========================
# SQLITE SETUP + MIGRATION
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

        # ---- MIGRATION: add neg_rep column if missing ----
        cols = [r[1] for r in conn.execute("PRAGMA table_info(reputation)").fetchall()]
        if "neg_rep" not in cols:
            conn.execute("ALTER TABLE reputation ADD COLUMN neg_rep INTEGER NOT NULL DEFAULT 0")
            conn.commit()

init_db()

# ========================
# DATABASE HELPERS
# ========================

def get_rep_data(user_id: int) -> tuple[int, int]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT rep, neg_rep FROM reputation WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return (row[0], row[1]) if row else (0, 0)

def set_rep_data(user_id: int, rep: int, neg_rep: int) -> None:
    # Keep rep non-negative; neg_rep non-negative
    rep = max(0, rep)
    neg_rep = max(0, neg_rep)

    with get_db() as conn:
        conn.execute("""
        INSERT INTO reputation (user_id, rep, neg_rep, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET rep = excluded.rep,
                      neg_rep = excluded.neg_rep,
                      updated_at = excluded.updated_at
        """, (user_id, rep, neg_rep, datetime.utcnow().isoformat()))
        conn.commit()

def add_positive_rep(user_id: int, amount: int = 1) -> tuple[int, int]:
    rep, neg = get_rep_data(user_id)
    rep += amount
    set_rep_data(user_id, rep, neg)
    return rep, neg

def add_negative_rep(user_id: int, amount: int = 1) -> tuple[int, int]:
    # IMPORTANT: does NOT subtract rep; only increments neg_rep
    rep, neg = get_rep_data(user_id)
    neg += amount
    set_rep_data(user_id, rep, neg)
    return rep, neg

def get_sorted_rep_items():
    # Sort by positive rep desc (your original behavior), then neg asc as a tiebreaker
    with get_db() as conn:
        return conn.execute(
            "SELECT user_id, rep, neg_rep FROM reputation ORDER BY rep DESC, neg_rep ASC"
        ).fetchall()

# ========================
# UTILITIES
# ========================

def get_trading_level(rep: int) -> int:
    return rep // REP_PER_LEVEL  # uncapped

def compact_stats(rep: int, neg: int) -> str:
    level = get_trading_level(rep)
    return f"👍 **{rep} Rep** • 👎 **{neg} Neg** • 🔰 **Lv. {level}**"

# ========================
# LEADERBOARD EMBED
# ========================

async def make_leaderboard_embed(items, page, guild, bot, viewer_id: int):
    total_pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    embed = discord.Embed(
        title="🏆 Reputation Leaderboard",
        color=discord.Color.gold(),
    )

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE

    for index, (user_id, rep_amount, neg_amount) in enumerate(items[start:end], start=start + 1):
        member = guild.get_member(user_id)
        if not member:
            try:
                member = await bot.fetch_user(user_id)
            except:
                member = None

        name = member.display_name if member else f"User ID {user_id}"
        medal = RANK_EMOJIS.get(index, f"`#{index}`")

        embed.add_field(
            name=f"{medal} {name}",
            value=compact_stats(rep_amount, neg_amount),
            inline=False,
        )

    # ---- YOUR STATS (kept from your original) ----
    viewer_rank = None
    for idx, (uid, _rep, _neg) in enumerate(items, start=1):
        if uid == viewer_id:
            viewer_rank = idx
            break

    viewer_rep, viewer_neg = get_rep_data(viewer_id)

    embed.add_field(
        name="━━━━━━━━━━\n👤 Your Stats",
        value=(
            f"🏅 **Rank:** {f'#{viewer_rank}' if viewer_rank else 'Unranked'}\n"
            f"{compact_stats(viewer_rep, viewer_neg)}"
        ),
        inline=False,
    )

    embed.set_footer(text=f"Page {page + 1}/{total_pages}")
    return embed

# ========================
# PAGINATION VIEW (RESTORED)
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
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self.update_buttons()
        embed = await make_leaderboard_embed(
            self.items, self.page, self.guild, self.bot, self.author_id
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
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
# COMMANDS (2-LINE RESPONSES)
# ========================

@bot.tree.command(name="rep", description="Give +1 reputation to a member")
@app_commands.checks.cooldown(1, 240)
async def rep(interaction: discord.Interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    rep_val, neg_val = add_positive_rep(member.id, 1)

    await interaction.response.send_message(
        f"🎖️ {interaction.user.mention} → {member.mention} **(+Rep)**\n"
        f"{compact_stats(rep_val, neg_val)}"
    )

@bot.tree.command(name="norep", description="Give negative rep (records Neg Rep; does not remove Rep)")
@app_commands.checks.cooldown(1, 240)
async def norep(interaction: discord.Interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    rep_val, neg_val = add_negative_rep(member.id, 1)

    await interaction.response.send_message(
        f"⚠️ {interaction.user.mention} → {member.mention} **(+Neg)**\n"
        f"{compact_stats(rep_val, neg_val)}"
    )

@bot.tree.command(name="checkrep", description="Check reputation, negative rep, and trading level")
async def checkrep(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    rep_val, neg_val = get_rep_data(member.id)

    await interaction.response.send_message(
        f"📊 {member.mention}\n"
        f"{compact_stats(rep_val, neg_val)}"
    )

@bot.tree.command(name="leaderboard", description="View the reputation leaderboard")
async def leaderboard(interaction: discord.Interaction):
    items = get_sorted_rep_items()
    if not items:
        return await interaction.response.send_message("📭 No reputation data yet.", ephemeral=True)

    embed = await make_leaderboard_embed(items, 0, interaction.guild, bot, interaction.user.id)
    view = LeaderboardView(items, interaction.guild, bot, interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view)

# ========================
# IMPORT / EXPORT (RESTORED + BACKCOMPAT)
# ========================

@bot.tree.command(name="importrep", description="Import reputation from JSON")
async def importrep(interaction: discord.Interaction, file: discord.Attachment):
    raw = await file.read()
    data = json.loads(raw)

    # Back-compat formats supported:
    # 1) {"123": 10, "456": 50}  -> rep only
    # 2) {"123": {"rep": 10, "neg_rep": 2}, ...}
    # 3) {"123": {"rep": 10, "neg": 2}, ...}  (loose key)
    with get_db() as conn:
        for uid_str, val in data.items():
            uid = int(uid_str)

            if isinstance(val, dict):
                rep_val = int(val.get("rep", 0))
                neg_val = int(val.get("neg_rep", val.get("neg", 0)))
            else:
                rep_val = int(val)
                neg_val = 0

            rep_val = max(0, rep_val)
            neg_val = max(0, neg_val)

            conn.execute("""
            INSERT INTO reputation (user_id, rep, neg_rep, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET rep = excluded.rep,
                          neg_rep = excluded.neg_rep,
                          updated_at = excluded.updated_at
            """, (uid, rep_val, neg_val, datetime.utcnow().isoformat()))
        conn.commit()

    await interaction.response.send_message("✅ Reputation imported.")

@bot.tree.command(name="exportrep", description="Export reputation to JSON")
async def exportrep(interaction: discord.Interaction):
    with get_db() as conn:
        rows = conn.execute("SELECT user_id, rep, neg_rep FROM reputation").fetchall()

    path = "/tmp/rep_export.json"
    payload = {str(uid): {"rep": rep, "neg_rep": neg} for uid, rep, neg in rows}
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)

    # Kept as ephemeral=True like your earlier version
    await interaction.response.send_message(
        "📦 Reputation export:",
        file=discord.File(path),
        ephemeral=True,
    )

# ========================
# RUN
# ========================

bot.run(TOKEN)
