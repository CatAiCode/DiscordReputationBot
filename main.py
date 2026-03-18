import json
import math
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# ========================
# CONFIG
# ========================

PAGE_SIZE = 10
DB_PATH = "/data/reputation.db"
REP_PER_LEVEL = 20
MAX_REP_PER_TARGET_PER_24H = 3
REP_HISTORY_PAGE_SIZE = 15

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
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS reputation (
            user_id INTEGER PRIMARY KEY,
            rep INTEGER NOT NULL,
            updated_at TEXT
        )
        """)
        conn.commit()

        cols = [r[1] for r in conn.execute("PRAGMA table_info(reputation)").fetchall()]
        if "neg_rep" not in cols:
            conn.execute(
                "ALTER TABLE reputation ADD COLUMN neg_rep INTEGER NOT NULL DEFAULT 0"
            )
            conn.commit()

        conn.execute("""
        CREATE TABLE IF NOT EXISTS rep_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            giver_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            given_at TEXT NOT NULL
        )
        """)
        conn.commit()

init_db()

# ========================
# DATABASE HELPERS
# ========================

def utc_now_iso():
    return datetime.utcnow().isoformat()

def get_rep_data(user_id: int) -> tuple[int, int]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT rep, neg_rep FROM reputation WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return (row[0], row[1]) if row else (0, 0)

def set_rep_data(user_id: int, rep: int, neg_rep: int):
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
        """, (user_id, rep, neg_rep, utc_now_iso()))
        conn.commit()

def add_positive_rep(user_id: int, amount: int = 1):
    rep, neg = get_rep_data(user_id)
    rep += amount
    set_rep_data(user_id, rep, neg)
    return rep, neg

def add_negative_rep(user_id: int, amount: int = 1):
    rep, neg = get_rep_data(user_id)
    neg += amount
    set_rep_data(user_id, rep, neg)
    return rep, neg

def get_sorted_rep_items():
    with get_db() as conn:
        return conn.execute("""
            SELECT user_id, rep, neg_rep
            FROM reputation
            ORDER BY rep DESC, neg_rep ASC, user_id ASC
        """).fetchall()

def get_rep_count_last_24h(giver_id: int, receiver_id: int) -> int:
    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    with get_db() as conn:
        row = conn.execute("""
            SELECT COUNT(*)
            FROM rep_history
            WHERE giver_id = ? AND receiver_id = ? AND given_at >= ?
        """, (giver_id, receiver_id, cutoff)).fetchone()
    return row[0] if row else 0

def log_rep_action(giver_id: int, receiver_id: int):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO rep_history (giver_id, receiver_id, given_at)
            VALUES (?, ?, ?)
        """, (giver_id, receiver_id, utc_now_iso()))
        conn.commit()

def can_give_rep(giver_id: int, receiver_id: int) -> tuple[bool, int]:
    used = get_rep_count_last_24h(giver_id, receiver_id)
    remaining = max(0, MAX_REP_PER_TARGET_PER_24H - used)
    return used < MAX_REP_PER_TARGET_PER_24H, remaining

def get_received_rep_history(receiver_id: int):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT giver_id, receiver_id, given_at
            FROM rep_history
            WHERE receiver_id = ?
            ORDER BY given_at DESC
        """, (receiver_id,)).fetchall()
    return rows

# ========================
# UTILITIES
# ========================

def get_trading_level(rep: int) -> int:
    return rep // REP_PER_LEVEL

def compact_stats(rep: int, neg: int) -> str:
    level = get_trading_level(rep)
    return f"👍 **{rep} Reputation** • 🔰 **Lv. {level}**"

def format_dt(dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return dt_str

async def resolve_user_name(guild: discord.Guild, bot: commands.Bot, user_id: int) -> str:
    member = guild.get_member(user_id) if guild else None
    if member:
        return member.display_name

    try:
        user = await bot.fetch_user(user_id)
        return user.name
    except Exception:
        return f"User {user_id}"

def build_rep_history_table(rows):
    lines = []
    lines.append(f"{'#':<4} {'From':<24} {'Date / Time':<20}")
    lines.append("-" * 52)

    for idx, (giver_name, given_at) in enumerate(rows, start=1):
        short_name = giver_name[:24]
        short_time = given_at[:20]
        lines.append(f"{idx:<4} {short_name:<24} {short_time:<20}")

    return "```" + "\n".join(lines) + "```"

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
            except Exception:
                member = None

        name = member.display_name if member else f"User ID {user_id}"
        medal = RANK_EMOJIS.get(index, f"`#{index}`")

        embed.add_field(
            name=f"{medal} {name}",
            value=compact_stats(rep_amount, neg_amount),
            inline=False,
        )

    viewer_rank = None
    for idx, (uid, _, _) in enumerate(items, start=1):
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
# PAGINATION VIEWS
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

class RepHistoryView(discord.ui.View):
    def __init__(self, member, pages, guild, bot, author_id):
        super().__init__(timeout=120)
        self.member = member
        self.pages = pages
        self.guild = guild
        self.bot = bot
        self.author_id = author_id
        self.page = 0
        self.max_pages = len(pages)
        self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.max_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ You can’t control someone else’s history viewer.",
                ephemeral=True,
            )
            return False
        return True

    async def make_embed(self):
        page_rows = self.pages[self.page]
        table = build_rep_history_table(page_rows)

        embed = discord.Embed(
            title=f"📜 Reputation History for {self.member.display_name}",
            description=(
                f"Showing positive reputation received by {self.member.mention}\n"
                f"Entries: **{sum(len(p) for p in self.pages)}**"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name=f"Page {self.page + 1}/{self.max_pages}",
            value=table,
            inline=False,
        )
        return embed

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self.update_buttons()
        embed = await self.make_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self.update_buttons()
        embed = await self.make_embed()
        await interaction.response.edit_message(embed=embed, view=self)

# ========================
# BOT SETUP
# ========================

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Logged in as {bot.user} | Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Command sync failed: {e}")
        print(f"Logged in as {bot.user}")

# ========================
# COMMAND ERROR HANDLER
# ========================

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"⏳ Slow down. Try again in **{error.retry_after:.1f} seconds**."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return

    print(f"Unhandled app command error: {error}")

    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "❌ Something went wrong while running that command.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Something went wrong while running that command.",
                ephemeral=True,
            )
    except Exception:
        pass

# ========================
# COMMANDS
# ========================

@bot.tree.command(name="rep", description="Give positive reputation")
@app_commands.checks.cooldown(1, 240)
async def rep(interaction: discord.Interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    await interaction.response.defer()

    try:
        allowed, remaining_before = can_give_rep(interaction.user.id, member.id)
        if not allowed:
            return await interaction.followup.send(
                f"❌ You have already given {member.mention} reputation "
                f"**{MAX_REP_PER_TARGET_PER_24H} times in the last 24 hours**.\n"
                f"Please wait before repping this user again.",
                ephemeral=True,
            )

        rep_val, neg_val = add_positive_rep(member.id)
        log_rep_action(interaction.user.id, member.id)

        used_now = MAX_REP_PER_TARGET_PER_24H - remaining_before + 1
        left_now = max(0, MAX_REP_PER_TARGET_PER_24H - used_now)

        await interaction.followup.send(
            f"{interaction.user.mention} repped {member.mention}\n"
            f"👍 {member.mention} now has **{rep_val}** reputation\n"
            f"🔰 Level: **{get_trading_level(rep_val)}**\n"
            f"🕒 You have used **{used_now}/{MAX_REP_PER_TARGET_PER_24H}** reps for this user in the last 24 hours, You can give up to 3 rep to the same user every 24 hours "
            f"({left_now} left)."
        )
    except Exception as e:
        print(f"/rep error: {e}")
        await interaction.followup.send(
            "❌ Something went wrong while giving reputation.",
            ephemeral=True,
        )

@bot.tree.command(name="norep", description="Give negative reputation (does not remove reputation)")
@app_commands.checks.cooldown(1, 240)
async def norep(interaction: discord.Interaction, member: discord.Member):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    await interaction.response.defer()

    try:
        rep_val, neg_val = add_negative_rep(member.id)

        await interaction.followup.send(
            f"{interaction.user.mention} gave negative reputation to {member.mention}\n"
            f"👍 {member.mention} still has **{rep_val}** positive reputation\n"
            f"👎 {member.mention} now has **{neg_val}** negative reputation"
        )
    except Exception as e:
        print(f"/norep error: {e}")
        await interaction.followup.send(
            "❌ Something went wrong while giving negative reputation.",
            ephemeral=True,
        )

@bot.tree.command(name="setrep", description="Set a member's reputation to a specific value")
async def setrep(interaction: discord.Interaction, member: discord.Member, reputation: int):
    if member.bot:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    _, neg_val = get_rep_data(member.id)
    set_rep_data(member.id, reputation, neg_val)

    rep_val, neg_val = get_rep_data(member.id)

    await interaction.response.send_message(
        f"🛠️ Reputation set by {interaction.user.mention} → {member.mention}\n"
        f"{compact_stats(rep_val, neg_val)}"
    )

@bot.tree.command(name="setnegativerep", description="Set a member's negative reputation to a specific value")
async def setnegativerep(interaction: discord.Interaction, member: discord.Member, negative_reputation: int):
    if member.bot:
        return await interaction.response.send_message("❌ Invalid target.", ephemeral=True)

    rep_val, _ = get_rep_data(member.id)
    set_rep_data(member.id, rep_val, negative_reputation)

    rep_val, neg_val = get_rep_data(member.id)

    await interaction.response.send_message(
        f"🛠️ Negative Reputation set by {interaction.user.mention} → {member.mention}\n"
        f"{compact_stats(rep_val, neg_val)}"
    )

@bot.tree.command(name="checkrep", description="Check reputation status")
async def checkrep(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    rep_val, neg_val = get_rep_data(member.id)

    await interaction.response.send_message(
        f"📊 {member.mention}\n"
        f"{compact_stats(rep_val, neg_val)}"
    )

@bot.tree.command(name="rephistory", description="View all positive rep history received by a member")
async def rephistory(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer()

    try:
        rows = get_received_rep_history(member.id)

        if not rows:
            return await interaction.followup.send(
                f"📭 {member.mention} has not received any positive reputation yet.",
                ephemeral=True,
            )

        formatted_rows = []
        for giver_id, receiver_id, given_at in rows:
            giver_name = await resolve_user_name(interaction.guild, bot, giver_id)
            formatted_rows.append((giver_name, format_dt(given_at)))

        chunks = [
            formatted_rows[i:i + REP_HISTORY_PAGE_SIZE]
            for i in range(0, len(formatted_rows), REP_HISTORY_PAGE_SIZE)
        ]

        first_table = build_rep_history_table(chunks[0])

        embed = discord.Embed(
            title=f"📜 Reputation History for {member.display_name}",
            description=(
                f"Showing positive reputation received by {member.mention}\n"
                f"Entries: **{len(formatted_rows)}**"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name=f"Page 1/{len(chunks)}",
            value=first_table,
            inline=False,
        )

        if len(chunks) == 1:
            return await interaction.followup.send(embed=embed)

        view = RepHistoryView(
            member=member,
            pages=chunks,
            guild=interaction.guild,
            bot=bot,
            author_id=interaction.user.id,
        )
        await interaction.followup.send(embed=embed, view=view)

    except Exception as e:
        print(f"/rephistory error: {e}")
        await interaction.followup.send(
            "❌ Something went wrong while loading reputation history.",
            ephemeral=True,
        )

@bot.tree.command(name="leaderboard", description="View the reputation leaderboard")
async def leaderboard(interaction: discord.Interaction):
    items = get_sorted_rep_items()
    if not items:
        return await interaction.response.send_message(
            "📭 No reputation data yet.",
            ephemeral=True
        )

    embed = await make_leaderboard_embed(items, 0, interaction.guild, bot, interaction.user.id)
    view = LeaderboardView(items, interaction.guild, bot, interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view)

# ========================
# IMPORT / EXPORT
# ========================

@bot.tree.command(name="importrep", description="Import reputation from JSON")
async def importrep(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True)

    try:
        raw = await file.read()
        data = json.loads(raw)

        if not isinstance(data, dict):
            return await interaction.followup.send(
                "❌ JSON file must contain an object/dictionary.",
                ephemeral=True
            )

        imported = 0

        with get_db() as conn:
            for uid_str, val in data.items():
                uid = int(uid_str)

                if isinstance(val, dict):
                    rep_val = int(val.get("reputation", val.get("rep", 0)))
                    neg_val = int(val.get("negative_reputation", val.get("neg_rep", 0)))
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
                """, (uid, rep_val, neg_val, utc_now_iso()))
                imported += 1

            conn.commit()

        await interaction.followup.send(
            f"✅ Reputation imported for **{imported}** user(s).",
            ephemeral=True
        )

    except Exception as e:
        print(f"/importrep error: {e}")
        await interaction.followup.send(
            "❌ Failed to import reputation JSON.",
            ephemeral=True
        )

@bot.tree.command(name="exportrep", description="Export reputation to JSON")
async def exportrep(interaction: discord.Interaction):
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT user_id, rep, neg_rep FROM reputation").fetchall()

        path = "/tmp/rep_export.json"
        payload = {
            str(uid): {
                "reputation": rep,
                "negative_reputation": neg,
            }
            for uid, rep, neg in rows
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        await interaction.response.send_message(
            "📦 Reputation export:",
            file=discord.File(path),
            ephemeral=True,
        )
    except Exception as e:
        print(f"/exportrep error: {e}")
        if interaction.response.is_done():
            await interaction.followup.send("❌ Failed to export reputation.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Failed to export reputation.", ephemeral=True)

# ========================
# RUN
# ========================

bot.run(TOKEN)
