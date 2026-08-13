from __future__ import annotations

import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from database import Database
from themes import THEMES, THEME_COUNT


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URI")
GUILD_ID = int(os.getenv("GUILD_ID", "1483821302463860798")) or None

ADMIN_ROLE_ID = 1537069353768329246

intents = discord.Intents.default()
intents.guilds = True

db = Database(DATABASE_URL)


def member_is_admin(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    return any(role.id == ADMIN_ROLE_ID for role in member.roles)


async def require_admin(interaction: discord.Interaction) -> bool:
    if member_is_admin(interaction):
        return True
    await interaction.response.send_message(
        "You need the **Admin** role or Discord's **Administrator** permission to use this command.",
        ephemeral=True,
    )
    return False


def clean_theme(theme: str) -> str:
    return " ".join(theme.split()).strip()


def chunk_lines(lines: list[str], limit: int = 3800) -> list[str]:
    chunks = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}".strip()
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class VoteSelect(discord.ui.Select):
    def __init__(self, round_id: int, suggestions):
        self.round_id = round_id
        options = [
            discord.SelectOption(
                label=row["theme"][:100],
                value=str(row["id"]),
                description=f"Current votes: {row['vote_count']}"[:100],
            )
            for row in suggestions[:25]
        ]
        super().__init__(
            placeholder="Choose one PFP theme…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        active = db.active_group_round()
        if not active or int(active["id"]) != self.round_id or active["status"] == "closed":
            await interaction.response.send_message(
                "This vote is no longer active.",
                ephemeral=True,
            )
            return

        suggestion_id = int(self.values[0])
        if not db.cast_vote(self.round_id, interaction.user.id, suggestion_id):
            await interaction.response.send_message(
                "That option is no longer available.",
                ephemeral=True,
            )
            return

        selected = next(
            (option.label for option in self.options if option.value == self.values[0]),
            "your selected theme",
        )
        await interaction.response.send_message(
            f"✅ Your vote is now **{selected}**.\nYou can change it any time before voting closes.",
            ephemeral=True,
        )


class VoteSelectView(discord.ui.View):
    def __init__(self, round_id: int, suggestions):
        super().__init__(timeout=180)
        self.add_item(VoteSelect(round_id, suggestions))


class PersistentVoteLauncher(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Vote",
        style=discord.ButtonStyle.primary,
        emoji="🗳️",
        custom_id="pfp:open_vote",
    )
    async def open_vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        active = db.active_group_round()
        if not active or active["status"] == "closed":
            await interaction.response.send_message(
                "There is no active PFP election right now.",
                ephemeral=True,
            )
            return

        suggestions = db.get_suggestions(int(active["id"]))
        if not suggestions:
            await interaction.response.send_message(
                "There are no themes to vote for.",
                ephemeral=True,
            )
            return

        if len(suggestions) > 25:
            suggestions = suggestions[:25]

        current = db.get_user_vote(int(active["id"]), interaction.user.id)
        note = (
            f"\nYour current vote: **{current['theme']}**."
            if current else ""
        )
        await interaction.response.send_message(
            f"Choose one theme below. You can change your vote until voting closes.{note}",
            view=VoteSelectView(int(active["id"]), suggestions),
            ephemeral=True,
        )


class PFPBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        self.add_view(PersistentVoteLauncher())

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"Synced {len(synced)} commands to guild {GUILD_ID}.")
        else:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} global commands.")


bot = PFPBot()
pfp = app_commands.Group(name="pfp", description="PFP theme tools")


# -------------------- Group theme election --------------------

@pfp.command(name="new", description="Open a new PFP theme suggestion round.")
async def pfp_new(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return

    active = db.active_group_round()
    if active:
        await interaction.response.send_message(
            f"A PFP election is already open (round #{active['id']}).",
            ephemeral=True,
        )
        return

    round_id = db.create_group_round()
    db.set_group_status(round_id, "open")
    embed = discord.Embed(
        title="🎭 New PFP Theme Election",
        description=(
            "The election is now open!\n\n"
            "💡 Use **/pfp suggest** to add your own idea.\n"
            "🎲 Use **/pfp random** if you want the bot to give you a random idea and add it automatically.\n"
            "🗳️ Press **Vote** below whenever you want to vote.\n\n"
            "You can change your vote at any time until an Admin closes the election."
        ),
    )
    embed.set_footer(text=f"Round #{round_id}")
    await interaction.response.send_message(embed=embed, view=PersistentVoteLauncher())


@pfp.command(name="suggest", description="Suggest a theme for the current PFP vote.")
@app_commands.describe(theme="The PFP theme you want to suggest")
async def pfp_suggest(interaction: discord.Interaction, theme: str):
    active = db.active_group_round()
    if not active:
        await interaction.response.send_message(
            "There is no active suggestion round.",
            ephemeral=True,
        )
        return
    if active["status"] == "closed":
        await interaction.response.send_message(
            "This PFP election is already closed.",
            ephemeral=True,
        )
        return

    if db.suggestion_count(int(active["id"])) >= 25:
        await interaction.response.send_message(
            "This election already has the maximum of **25 themes**.",
            ephemeral=True,
        )
        return

    theme = clean_theme(theme)
    if len(theme) < 2 or len(theme) > 100:
        await interaction.response.send_message(
            "Theme names must be between 2 and 100 characters.",
            ephemeral=True,
        )
        return

    if not db.add_suggestion(int(active["id"]), theme, interaction.user.id):
        await interaction.response.send_message(
            "That theme is already in this round or has already been used before.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✅ **{theme}** has been added to the suggestions.",
    )


@pfp.command(name="suggestions", description="Show the themes suggested in the current round.")
async def pfp_suggestions(interaction: discord.Interaction):
    active = db.active_group_round()
    if not active:
        await interaction.response.send_message(
            "There is no active PFP round.",
            ephemeral=True,
        )
        return

    rows = db.get_suggestions(int(active["id"]))
    if not rows:
        await interaction.response.send_message(
            "No themes have been suggested yet.",
            ephemeral=True,
        )
        return

    lines = [
        f"**{i}. {row['theme']}** — {row['vote_count']} vote{'s' if row['vote_count'] != 1 else ''}"
        for i, row in enumerate(rows, 1)
    ]
    chunks = chunk_lines(lines)
    embeds = []
    for index, chunk in enumerate(chunks):
        embed = discord.Embed(
            title="🎭 PFP Theme Suggestions" if index == 0 else "PFP Theme Suggestions (continued)",
            description=chunk,
        )
        if index == 0:
            embed.set_footer(text=f"Round #{active['id']} • {len(rows)} themes")
        embeds.append(embed)

    await interaction.response.send_message(embeds=embeds, ephemeral=True)


@pfp.command(name="close", description="Close the current vote and announce the winning PFP theme.")
async def pfp_close(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return

    active = db.active_group_round()
    if not active:
        await interaction.response.send_message(
            "There is no active PFP round.",
            ephemeral=True,
        )
        return
    result = db.close_group_round(int(active["id"]))
    if not result:
        await interaction.response.send_message(
            "This round has no suggestions.",
            ephemeral=True,
        )
        return

    description = (
        f"🏆 **{result['theme']}** wins with **{result['votes']} vote"
        f"{'s' if result['votes'] != 1 else ''}**!"
    )
    if result["tie"]:
        tied = ", ".join(result["finalists"])
        description += (
            f"\n\nThere was a tie between **{tied}**. "
            "The bot used a random tie-break between the tied themes."
        )

    embed = discord.Embed(
        title="🎉 PFP Theme Selected",
        description=description,
    )
    embed.set_footer(text=f"Round #{active['id']}")
    await interaction.response.send_message(embed=embed)


@pfp.command(name="history", description="Show previously selected group PFP themes.")
async def pfp_history(interaction: discord.Interaction):
    rows = db.group_history(20)
    if not rows:
        await interaction.response.send_message(
            "No group PFP themes have been completed yet.",
            ephemeral=True,
        )
        return

    lines = [
        f"**{row['theme']}** — {row['votes']} vote{'s' if row['votes'] != 1 else ''}"
        for row in rows
    ]
    embed = discord.Embed(
        title="📚 PFP Theme History",
        description="\n".join(lines),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# -------------------- Random suggestion helper --------------------

@pfp.command(name="random", description="Add one unique random theme to the current PFP election.")
async def pfp_random(interaction: discord.Interaction):
    active = db.active_group_round()
    if not active or active["status"] == "closed":
        await interaction.response.send_message(
            "There is no active PFP election. An Admin must start one with **/pfp new**.",
            ephemeral=True,
        )
        return

    theme = db.add_random_suggestion(
        int(active["id"]),
        interaction.user.id,
        THEMES,
    )
    if not theme:
        await interaction.response.send_message(
            "I couldn't add another random theme. The election may already have 25 suggestions, or the unused theme pool may be empty.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="🎲 Random Theme Suggestion",
        description=(
            f"# {theme}\n\n"
            "This theme has been **added to the current election** and will not be randomly selected again in this round."
        ),
    )
    embed.set_footer(text=f"Round #{active['id']}")
    await interaction.response.send_message(embed=embed)


# -------------------- Theme bank administration --------------------

@pfp.command(name="stats", description="Show PFP theme library statistics.")
async def pfp_stats(interaction: discord.Interaction):
    stats = db.theme_stats(THEME_COUNT)
    total_pool = stats["built_in"] + stats["custom"]
    approx_available = max(0, total_pool - stats["used"] - stats["disabled"])

    embed = discord.Embed(
        title="📊 PFP Theme Bank",
        description=(
            f"**Built-in themes:** {stats['built_in']}\n"
            f"**Custom themes:** {stats['custom']}\n"
            f"**Used themes:** {stats['used']}\n"
            f"**Disabled themes:** {stats['disabled']}\n"
            f"**Approx. unused themes:** {approx_available}"
        ),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@pfp.command(name="theme-add", description="Add a custom theme to the random theme bank.")
@app_commands.describe(theme="Theme to add")
async def pfp_theme_add(interaction: discord.Interaction, theme: str):
    if not await require_admin(interaction):
        return

    if db.suggestion_count(int(active["id"])) >= 25:
        await interaction.response.send_message(
            "This election already has the maximum of **25 themes**.",
            ephemeral=True,
        )
        return

    theme = clean_theme(theme)
    if len(theme) < 2 or len(theme) > 100:
        await interaction.response.send_message(
            "Theme names must be between 2 and 100 characters.",
            ephemeral=True,
        )
        return

    created = db.add_custom_theme(theme)
    if created:
        await interaction.response.send_message(
            f"✅ **{theme}** was added to the theme bank.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"**{theme}** was already in the custom bank. It has been enabled.",
            ephemeral=True,
        )


@pfp.command(name="theme-disable", description="Prevent a theme from being randomly assigned.")
@app_commands.describe(theme="Exact theme name to disable")
async def pfp_theme_disable(interaction: discord.Interaction, theme: str):
    if not await require_admin(interaction):
        return

    theme = clean_theme(theme)
    db.disable_theme(theme)
    await interaction.response.send_message(
        f"⛔ **{theme}** is now disabled.",
        ephemeral=True,
    )


@pfp.command(name="theme-enable", description="Allow a disabled theme to be randomly assigned again.")
@app_commands.describe(theme="Exact theme name to enable")
async def pfp_theme_enable(interaction: discord.Interaction, theme: str):
    if not await require_admin(interaction):
        return

    theme = clean_theme(theme)
    db.enable_theme(theme)
    await interaction.response.send_message(
        f"✅ **{theme}** is enabled.",
        ephemeral=True,
    )


@pfp.command(name="recycle-themes", description="Allow all historical themes to be used again. Admin only.")
@app_commands.describe(confirm="You must set this to True to recycle the used-theme history")
async def pfp_recycle_themes(interaction: discord.Interaction, confirm: bool):
    if not await require_admin(interaction):
        return

    if not confirm:
        await interaction.response.send_message(
            "Nothing was changed. Set **confirm** to `True` only if you intentionally want old themes to become available again.",
            ephemeral=True,
        )
        return

    count = db.recycle_used_history()
    await interaction.response.send_message(
        f"♻️ Recycled **{count}** used themes. Historical group results are still kept, but those themes can now be assigned again.",
        ephemeral=True,
    )


bot.tree.add_command(pfp)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id if bot.user else 'unknown'})")
    print(f"Built-in PFP themes loaded: {THEME_COUNT}")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Add it to your environment variables.")
    bot.run(TOKEN)
