import json
import math
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import os

# ------------------------
# CONFIG
# ------------------------

MIN_ACCOUNT_AGE_DAYS = 7            # Min account age to use rep/norep
PAGE_SIZE = 10                      # Users per leaderboard page

# Use Railway volume at /data
DATA_FILE = Path("/data/rep_data.json")
DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

# ------------------------
# LOAD TOKEN FROM .env
# ------------------------

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN is missing in the .env file!")

# ------------------------
# REPUTATION STORAGE
# ------------------------

def load_rep() -> dict:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

def save_rep(rep: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(rep, f)

reputation: dict[str, int] = load_rep()

def set_rep(user_id: int, amount: int) -> int:
    uid = str(user_id)
    reputation[uid] = int(amount)
    save_rep(reputation)
    return int(amount)

def add_rep(user_id: int, amount: int) -> int:
    uid = str(user_id)
    reputation[uid] = reputation.get(uid, 0) + amount
    save_rep(reputation)
    return reputation[uid]

def get_rep(user_id: int) -> int:
    return reputation.get(str(user_id), 0)

def get_sorted_rep_items() -> list[tuple[str, int]]:
    return sorted(reputation.items(), key=lambda item: item[1], reverse=True)

# ------------------------
# ACCOUNT AGE CHECK
# ------------------------

def meets_account_age_requirement(user: discord.abc.User) -> bool:
    account_age = datetime.now(timezone.utc) - user.created_at
    return account_age >= timedelta(days=MIN_ACCOUNT_AGE_DAYS)

# ------------------------
# LEADERBOARD HELPERS
# ------------------------

def make_leaderboard_embed(sorted_items, page, guild):
    total_entries = len(sorted_items)
    total_pages = max(1, math.ceil(total_entries / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = sorted_items[start:end]

    lines = []
    for index, (user_id_str, rep_amount) in enumerate(page_items, start=start + 1):
        user_id = int(user_id_str)
        member = guild.get_member(user_id)
        name = member.mention if member else f"<@{user_id}>"
        lines.append(f"**#{index}** — {name}: **{rep_amount}** rep")

    embed = discord.Embed(
        title="🏆 Reputation Leaderboard",
        description="\n".join(lines) if lines else "📭 No reputation data yet!",
    )
    embed.set_footer(text=f"Page {page + 1}/{total_pages} • Total users: {total_entries}")
    return embed

class LeaderboardView(discord.ui.View):
    def __init__(self, sorted_items, guild, author_id, start_page=0):
        super().__init__(timeout=120)
        self.sorted_items = sorted_items
        self.guild = guild
        self.author_id = author_id
        self.current_page = start_page
        self.update_button_states()

    def update_button_states(self):
        total_pages = max(1, math.ceil(len(self.sorted_items) / PAGE_SIZE))
        self.children[0].disabled = self.current_page <= 0
        self.children[1].disabled = self.current_page >= total_pages - 1

    async def update_message(self, interaction):
        self.update_button_states()
        embed = make_leaderboard_embed(self.sorted_items, self.current_page, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "Only the user who ran `/leaderboard` can use these buttons.",
                ephemeral=True,
            )
        if self.current_page > 0:
            self.current_page -= 1
            await self.update_message(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "Only the user who ran `/leaderboard` can use these buttons.",
                ephemeral=True,
            )
        total_pages = max(1, math.ceil(len(self.sorted_items) / PAGE_SIZE))
        if self.current_page < total_pages - 1:
            self.current_page += 1
            await self.update_message(interaction)

# ------------------------
# BOT SETUP
# ------------------------

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print(f"Sync error: {e}")

# -----------------------------------
# /rep (+1) — 4 MIN COOLDOWN
# -----------------------------------

@bot.tree.command(name="rep", description="Give +1 reputation to a user.")
@app_commands.describe(member="Who gets the rep?")
@app_commands.checks.cooldown(1, 240)  # 4 minutes
async def rep(interaction, member: discord.Member):

    user = interaction.user

    if not meets_account_age_requirement(user):
        days = (datetime.now(timezone.utc) - user.created_at).days
        return await interaction.response.send_message(
            f"❌ Account too new. Must be **7 days** old.\nYour age: **{days} days**",
            ephemeral=True,
        )

    if member.id == user.id:
        return await interaction.response.send_message("❌ You can't rep yourself.", ephemeral=True)

    if member.bot:
        return await interaction.response.send_message("🤖 You can't rep bots.", ephemeral=True)

    new_val = add_rep(member.id, 1)

    await interaction.response.send_message(
        f"👍 {user.mention} gave **+1 rep** to {member.mention}!\n⭐ New rep: **{new_val}**"
    )

@rep.error
async def rep_error(interaction, error):
    if isinstance(error, app_commands.CommandOnCooldown):
        secs = int(error.retry_after)
        await interaction.response.send_message(
            f"⏳ Cooldown: Try again in **{secs} seconds**.",
            ephemeral=True,
        )

# -----------------------------------
# /norep (-1) — 4 MIN COOLDOWN
# -----------------------------------

@bot.tree.command(name="norep", description="Subtract 1 reputation from a user.")
@app_commands.describe(member="Who loses rep?")
@app_commands.checks.cooldown(1, 240)  # 4 minutes
async def norep(interaction, member: discord.Member):
    user = interaction.user

    if not meets_account_age_requirement(user):
        days = (datetime.now(timezone.utc) - user.created_at).days
        return await interaction.response.send_message(
            f"❌ Account too new. Must be **7 days** old.\nYour age: **{days} days**",
            ephemeral=True,
        )

    if member.id == user.id:
        return await interaction.response.send_message("❌ You can't remove rep from yourself.", ephemeral=True)

    if member.bot:
        return await interaction.response.send_message("🤖 You can't remove rep from bots.", ephemeral=True)

    new_val = add_rep(member.id, -1)

    await interaction.response.send_message(
        f"⚠️ {user.mention} gave **-1 rep** to {member.mention}.\n⭐ New rep: **{new_val}**"
    )

@norep.error
async def norep_error(interaction, error):
    if isinstance(error, app_commands.CommandOnCooldown):
        secs = int(error.retry_after)
        await interaction.response.send_message(
            f"⏳ Cooldown: Try again in **{secs} seconds**.",
            ephemeral=True,
        )

# -----------------------------------
# /setrep
# -----------------------------------

@bot.tree.command(name="setrep", description="Set a user's reputation to an exact number.")
async def setrep(interaction, member: discord.Member, amount: int):

    if member.bot:
        return await interaction.response.send_message("🤖 You can't set rep for bots.", ephemeral=True)

    if interaction.user.id == member.id:
        return await interaction.response.send_message("❌ You cannot set your own reputation.", ephemeral=True)

    if amount < -1000 or amount > 1000:
        return await interaction.response.send_message(
            "⚠️ Amount must be between **-1000** and **1000**.",
            ephemeral=True,
        )

    new_val = set_rep(member.id, amount)

    await interaction.response.send_message(
        f"🛠️ {interaction.user.mention} set {member.mention}'s rep to **{new_val}**."
    )

# -----------------------------------
# /checkrep
# -----------------------------------

@bot.tree.command(name="checkrep", description="Check a user's reputation.")
async def checkrep(interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    amount = get_rep(member.id)

    await interaction.response.send_message(
        f"📊 {member.mention} has **{amount}** rep."
    )

# -----------------------------------
# /leaderboard
# -----------------------------------

@bot.tree.command(name="leaderboard", description="Show the reputation leaderboard.")
async def leaderboard(interaction):
    if not reputation:
        return await interaction.response.send_message("📭 No rep data yet!", ephemeral=True)

    sorted_items = get_sorted_rep_items()
    embed = make_leaderboard_embed(sorted_items, 0, interaction.guild)
    view = LeaderboardView(sorted_items, interaction.guild, interaction.user.id)

    await interaction.response.send_message(embed=embed, view=view)

# -----------------------------------
# /backuprep (anyone can run)
# -----------------------------------

@bot.tree.command(name="backuprep", description="Download the reputation data file.")
async def backuprep(interaction):
    if not DATA_FILE.exists():
        return await interaction.response.send_message(
            "❌ No rep data exists yet.",
            ephemeral=True,
        )

    await interaction.response.send_message(
        "📁 Here is the current **rep_data.json**:",
        file=discord.File(str(DATA_FILE)),
        ephemeral=True,
    )

# ------------------------
# RUN BOT
# ------------------------

bot.run(TOKEN)
