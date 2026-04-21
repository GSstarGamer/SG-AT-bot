from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from roblox_api import RobloxAPIError, RobloxClient, RobloxUser


load_dotenv()

EMBED_COLOR = discord.Color.from_rgb(128, 0, 255)
ALLY_EMBED_COLOR = discord.Color.from_rgb(57, 255, 20)
TEAMER_EMBED_COLOR = discord.Color.red()
CONFIG_PATH = Path("guild_config.json")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("sg_at_bot")
MARKDOWN_URL_PATTERN = re.compile(r"\((https?://[^)]+)\)")


def load_guild_config() -> dict[str, dict[str, Any]]:
    if not CONFIG_PATH.exists():
        return {}

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if isinstance(data, dict):
        return data
    return {}


def save_guild_config(config: dict[str, dict[str, Any]]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=2, sort_keys=True)


def get_guild_state(bot: "ATBot", guild_id: int) -> dict[str, Any]:
    state = bot.guild_config.setdefault(str(guild_id), {})
    state.setdefault("reporter_ids", {})
    state.setdefault("reporter_usernames", {})
    state.setdefault("report_results", {})
    state.setdefault("ally_usernames", {})
    state.setdefault("removed_rep_user_ids", {})
    state.setdefault("user_memory", {})
    return state


def get_user_memory_record(guild_state: dict[str, Any], discord_user_id: int) -> dict[str, Any]:
    key = str(discord_user_id)
    entry = guild_state["user_memory"].get(key)

    if isinstance(entry, dict):
        entry.setdefault("rep", 0)
        guild_state["user_memory"][key] = entry
        return entry

    if isinstance(entry, str):
        upgraded = {
            "value": entry,
            "username": entry,
            "rep": 0,
        }
        guild_state["user_memory"][key] = upgraded
        return upgraded

    created = {"rep": 0}
    guild_state["user_memory"][key] = created
    return created


def get_saved_user_entry(guild_state: dict[str, Any], discord_user_id: int) -> dict[str, str] | None:
    entry = get_user_memory_record(guild_state, discord_user_id)
    if isinstance(entry, dict):
        value = entry.get("value")
        username = entry.get("username")
        if isinstance(value, str) and isinstance(username, str):
            return {"value": value, "username": username}
    return None


def set_saved_user_entry(
    guild_state: dict[str, Any],
    discord_user_id: int,
    roblox_user: RobloxUser,
) -> None:
    entry = get_user_memory_record(guild_state, discord_user_id)
    entry["value"] = roblox_user.profile_url
    entry["username"] = roblox_user.username


def build_panel_embed(forum_channel_mention: str) -> discord.Embed:
    return discord.Embed(
        title="AT report",
        description=(
            "create a report if being your being teamed on. "
            "Everyone under AT squad will be pinged. "
            f"This report will go into {forum_channel_mention}"
        ),
        color=EMBED_COLOR,
    )


def build_report_embeds(
    reporter: RobloxUser,
    reporter_discord_username: str | None,
    allies: list[RobloxUser],
    ally_discord_usernames: list[str | None],
    teamers: list[RobloxUser],
) -> list[discord.Embed]:
    reporter_title = "Reporter"
    if reporter_discord_username:
        reporter_title = f"Reporter - {reporter_discord_username}"

    summary_embed = discord.Embed(
        title=reporter_title,
        description=f"[{reporter.label}]({reporter.profile_url})",
        color=ALLY_EMBED_COLOR,
    )
    if reporter.avatar_url:
        summary_embed.set_thumbnail(url=reporter.avatar_url)

    embeds = [summary_embed]
    for index, ally in enumerate(allies):
        ally_title = "Ally"
        ally_discord_username = (
            ally_discord_usernames[index]
            if index < len(ally_discord_usernames)
            else None
        )
        if ally_discord_username:
            ally_title = f"Ally - {ally_discord_username}"
        embed = discord.Embed(
            title=ally_title,
            description=f"[{ally.label}]({ally.profile_url})",
            color=ALLY_EMBED_COLOR,
        )
        if ally.avatar_url:
            embed.set_thumbnail(url=ally.avatar_url)
        embeds.append(embed)

    for teamer in teamers:
        embed = discord.Embed(
            title="Teamer",
            description=f"[{teamer.label}]({teamer.profile_url})",
            color=TEAMER_EMBED_COLOR,
        )
        if teamer.avatar_url:
            embed.set_thumbnail(url=teamer.avatar_url)
        embeds.append(embed)

    return embeds


def build_report_thread_title(
    reporter: RobloxUser,
    allies: list[RobloxUser],
    teamers: list[RobloxUser],
) -> str:
    return f"{1 + len(allies)}v{len(teamers)} from {reporter.display_name}"[:100]


def get_report_identity(
    guild: discord.Guild,
    guild_state: dict[str, Any],
    thread_id: int,
) -> tuple[str | None, list[int], list[str | None]]:
    reporter_ids = guild_state["reporter_ids"]
    reporter_usernames = guild_state.setdefault("reporter_usernames", {})
    ally_user_ids_map = guild_state.setdefault("ally_user_ids", {})
    ally_usernames_map = guild_state.setdefault("ally_usernames", {})
    ally_discord_ids = ally_user_ids_map.get(str(thread_id), [])
    reporter_discord_id = reporter_ids.get(str(thread_id))
    reporter_discord_username = reporter_usernames.get(str(thread_id))

    if reporter_discord_username is None:
        reporter_member = (
            guild.get_member(reporter_discord_id)
            if isinstance(reporter_discord_id, int)
            else None
        )
        reporter_discord_username = reporter_member.name if reporter_member else None

    ally_discord_usernames: list[str | None] = []
    stored_ally_usernames = ally_usernames_map.get(str(thread_id), [])
    for index, ally_discord_id in enumerate(ally_discord_ids):
        stored_username = (
            stored_ally_usernames[index]
            if index < len(stored_ally_usernames)
            else None
        )
        if stored_username is not None:
            ally_discord_usernames.append(stored_username)
            continue

        member = (
            guild.get_member(ally_discord_id)
            if isinstance(ally_discord_id, int)
            else None
        )
        ally_discord_usernames.append(member.name if member else None)
    return reporter_discord_username, ally_discord_ids, ally_discord_usernames


def has_setup_permissions(member: discord.Member) -> bool:
    permissions = member.guild_permissions
    return all(
        (
            permissions.attach_files,
            permissions.create_public_threads,
            permissions.embed_links,
            permissions.manage_roles,
            permissions.mention_everyone,
            permissions.read_message_history,
            permissions.send_messages,
            permissions.send_messages_in_threads,
            permissions.use_application_commands,
            permissions.view_channel,
        )
    )


def extract_first_url(text: str | None) -> str | None:
    if not text:
        return None

    match = MARKDOWN_URL_PATTERN.search(text)
    if match is None:
        return None
    return match.group(1)


async def parse_report_message(
    bot: "ATBot",
    message: discord.Message,
) -> tuple[RobloxUser, list[RobloxUser], list[RobloxUser]]:
    if not message.embeds:
        raise ValueError("This report does not have any embeds to update.")

    reporter_url = extract_first_url(message.embeds[0].description)
    if reporter_url is None:
        raise ValueError("Could not read the reporter from the first embed.")

    reporter = await bot.roblox.resolve_user(reporter_url)
    allies: list[RobloxUser] = []
    teamers: list[RobloxUser] = []

    for embed in message.embeds[1:]:
        player_url = extract_first_url(embed.description)
        if player_url is None:
            continue
        player = await bot.roblox.resolve_user(player_url)
        if embed.color == ALLY_EMBED_COLOR:
            allies.append(player)
        else:
            teamers.append(player)

    return reporter, allies, teamers


async def append_teamers_to_report(
    bot: "ATBot",
    thread: discord.Thread,
    raw_players: list[str],
) -> tuple[RobloxUser, list[RobloxUser], list[RobloxUser], list[RobloxUser]]:
    report_message = await get_report_message(thread)
    reporter, allies, existing_teamers = await parse_report_message(bot, report_message)
    guild_state = get_guild_state(bot, thread.guild.id)
    reporter_discord_username, ally_discord_ids, ally_discord_usernames = (
        get_report_identity(thread.guild, guild_state, thread.id)
    )

    added_teamers: list[RobloxUser] = []
    existing_ids = {reporter.user_id}
    existing_ids.update(ally.user_id for ally in allies)
    existing_ids.update(teamer.user_id for teamer in existing_teamers)

    for raw_player in raw_players:
        new_teamer = await bot.roblox.resolve_user(raw_player)
        if new_teamer.user_id in existing_ids:
            continue
        existing_teamers.append(new_teamer)
        added_teamers.append(new_teamer)
        existing_ids.add(new_teamer.user_id)

    if not added_teamers:
        raise ValueError("All of those players are already listed in this report.")

    updated_embeds = build_report_embeds(
        reporter,
        reporter_discord_username,
        allies,
        ally_discord_usernames,
        existing_teamers,
    )
    await report_message.edit(
        embeds=updated_embeds[:10],
        view=ReportActionView(reporter.join_url),
    )
    await thread.edit(name=build_report_thread_title(reporter, allies, existing_teamers))

    return reporter, allies, existing_teamers, added_teamers


async def append_allies_to_report(
    bot: "ATBot",
    thread: discord.Thread,
    raw_players: list[str],
    ally_discord_id: int | None,
) -> tuple[RobloxUser, list[RobloxUser], list[RobloxUser], list[RobloxUser]]:
    report_message = await get_report_message(thread)
    reporter, existing_allies, teamers = await parse_report_message(bot, report_message)
    guild_state = get_guild_state(bot, thread.guild.id)
    ally_user_ids_map = guild_state.setdefault("ally_user_ids", {})
    ally_discord_ids = ally_user_ids_map.setdefault(str(thread.id), [])
    ally_usernames_map = guild_state.setdefault("ally_usernames", {})
    ally_discord_usernames_store = ally_usernames_map.setdefault(str(thread.id), [])
    removed_rep_user_ids = guild_state.setdefault("removed_rep_user_ids", {}).get(
        str(thread.id),
        [],
    )
    reporter_discord_username, _, _ = get_report_identity(
        thread.guild,
        guild_state,
        thread.id,
    )

    if ally_discord_id is not None and ally_discord_id in removed_rep_user_ids:
        raise ValueError("That user cannot join this report again after having rep removed.")

    added_allies: list[RobloxUser] = []
    existing_ids = {reporter.user_id}
    existing_ids.update(ally.user_id for ally in existing_allies)
    existing_ids.update(teamer.user_id for teamer in teamers)

    for raw_player in raw_players:
        new_ally = await bot.roblox.resolve_user(raw_player)
        if new_ally.user_id in existing_ids:
            continue
        existing_allies.append(new_ally)
        added_allies.append(new_ally)
        existing_ids.add(new_ally.user_id)

    if not added_allies:
        raise ValueError("All of those players are already listed in this report.")

    if ally_discord_id is not None:
        ally_discord_ids.extend([ally_discord_id] * len(added_allies))
        ally_discord_usernames_store.extend(
            [thread.guild.get_member(ally_discord_id).name if thread.guild.get_member(ally_discord_id) else None]
            * len(added_allies)
        )
        save_guild_config(bot.guild_config)

    _, _, ally_discord_usernames = get_report_identity(
        thread.guild,
        guild_state,
        thread.id,
    )

    updated_embeds = build_report_embeds(
        reporter,
        reporter_discord_username,
        existing_allies,
        ally_discord_usernames,
        teamers,
    )
    await report_message.edit(
        embeds=updated_embeds[:10],
        view=ReportActionView(reporter.join_url),
    )
    await thread.edit(name=build_report_thread_title(reporter, existing_allies, teamers))

    return reporter, existing_allies, teamers, added_allies


async def remove_ally_from_report(
    bot: "ATBot",
    thread: discord.Thread,
    ally_discord_id: int,
    reporter_discord_id: int,
) -> tuple[RobloxUser, RobloxUser]:
    report_message = await get_report_message(thread)
    reporter, existing_allies, teamers = await parse_report_message(bot, report_message)
    guild_state = get_guild_state(bot, thread.guild.id)
    reporter_discord_username, ally_discord_ids, ally_discord_usernames = (
        get_report_identity(thread.guild, guild_state, thread.id)
    )
    removed_rep_user_ids_map = guild_state.setdefault("removed_rep_user_ids", {})
    removed_rep_user_ids = removed_rep_user_ids_map.setdefault(str(thread.id), [])
    ally_usernames_map = guild_state.setdefault("ally_usernames", {})
    ally_discord_usernames_store = ally_usernames_map.setdefault(str(thread.id), [])

    if ally_discord_id in removed_rep_user_ids:
        raise ValueError("You have already removed rep from that ally in this report.")

    try:
        ally_index = ally_discord_ids.index(ally_discord_id)
    except ValueError as error:
        raise ValueError("That ally is not in this report.") from error

    if ally_index >= len(existing_allies):
        raise ValueError("Could not match that ally to the current report list.")

    removed_ally = existing_allies.pop(ally_index)
    ally_discord_ids.pop(ally_index)
    if ally_index < len(ally_discord_usernames):
        ally_discord_usernames.pop(ally_index)
    if ally_index < len(ally_discord_usernames_store):
        ally_discord_usernames_store.pop(ally_index)

    removed_rep_user_ids.append(ally_discord_id)

    user_record = get_user_memory_record(guild_state, ally_discord_id)
    current_rep = user_record.get("rep", 0)
    if not isinstance(current_rep, int):
        current_rep = 0
    user_record["rep"] = current_rep - 1
    save_guild_config(bot.guild_config)

    updated_embeds = build_report_embeds(
        reporter,
        reporter_discord_username,
        existing_allies,
        ally_discord_usernames,
        teamers,
    )
    await report_message.edit(
        embeds=updated_embeds[:10],
        view=ReportActionView(reporter.join_url),
    )
    await thread.edit(name=build_report_thread_title(reporter, existing_allies, teamers))

    member = thread.guild.get_member(ally_discord_id)
    if member is None:
        try:
            member = await thread.guild.fetch_member(ally_discord_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member = None

    if member is not None:
        try:
            await member.send(
                f"<@{reporter_discord_id}> thinks you were not helpful, so you lost 1 rep. "
                "If you think this is false, please report this user and you may get your rep back."
            )
        except discord.Forbidden:
            logger.warning(
                "Discord blocked the removerep DM to ally %s (%s) in guild %s.",
                member.name,
                member.id,
                thread.guild.id,
            )
        except discord.HTTPException as error:
            logger.error(
                "Failed to send removerep DM to ally %s (%s) in guild %s: %s",
                member.name,
                member.id,
                thread.guild.id,
                error,
            )

    return reporter, removed_ally


async def send_resolution_dms(
    guild: discord.Guild,
    reporter_discord_id: int | None,
    ally_discord_ids: list[int],
    won_fight: bool,
) -> None:
    if reporter_discord_id is None:
        logger.warning(
            "Skipping resolution DMs in guild %s because reporter_discord_id is missing.",
            guild.id,
        )
        return

    reporter_mention = f"<@{reporter_discord_id}>"
    unique_ally_ids = list(dict.fromkeys(ally_discord_ids))
    logger.info(
        "Preparing resolution DMs in guild %s for reporter %s. Allies: %s. Won: %s",
        guild.id,
        reporter_discord_id,
        unique_ally_ids,
        won_fight,
    )

    for ally_discord_id in unique_ally_ids:
        member = guild.get_member(ally_discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(ally_discord_id)
                logger.info(
                    "Fetched uncached ally %s in guild %s before sending DM.",
                    ally_discord_id,
                    guild.id,
                )
            except discord.NotFound:
                logger.warning(
                    "Could not DM ally %s in guild %s because the member could not be found.",
                    ally_discord_id,
                    guild.id,
                )
                continue
            except discord.Forbidden:
                logger.warning(
                    "Could not fetch ally %s in guild %s due to missing permissions.",
                    ally_discord_id,
                    guild.id,
                )
                continue
            except discord.HTTPException as error:
                logger.error(
                    "Failed to fetch ally %s in guild %s before sending DM: %s",
                    ally_discord_id,
                    guild.id,
                    error,
                )
                continue

        if won_fight:
            message = (
                f"Thank you for helping out {reporter_mention}. "
                "You have gained +1 rep. ❤️"
            )
        else:
            message = (
                f"Thank you for helping out {reporter_mention}. "
                "Unfortunately, we lost this fight. We appreciate the effort! ❤️"
            )

        try:
            await member.send(message)
            logger.info(
                "Sent resolution DM to ally %s (%s) in guild %s.",
                member.name,
                member.id,
                guild.id,
            )
        except discord.Forbidden:
            logger.warning(
                "Discord blocked the resolution DM to ally %s (%s) in guild %s.",
                member.name,
                member.id,
                guild.id,
            )
            continue
        except discord.HTTPException as error:
            logger.error(
                "Failed to send resolution DM to ally %s (%s) in guild %s: %s",
                member.name,
                member.id,
                guild.id,
                error,
            )


async def resolve_report(
    bot: "ATBot",
    thread: discord.Thread,
    won_fight: bool,
) -> None:
    guild_state = get_guild_state(bot, thread.guild.id)
    reporter_ids = guild_state["reporter_ids"]
    ally_user_ids_map = guild_state.setdefault("ally_user_ids", {})
    report_results = guild_state["report_results"]

    thread_key = str(thread.id)
    if thread_key in report_results:
        raise ValueError("This report has already been closed.")

    reporter_discord_id = reporter_ids.get(thread_key)
    stored_ally_ids = ally_user_ids_map.get(thread_key, [])
    ally_discord_ids = [
        ally_id for ally_id in stored_ally_ids if isinstance(ally_id, int)
    ]

    if won_fight:
        for ally_discord_id in dict.fromkeys(ally_discord_ids):
            user_record = get_user_memory_record(guild_state, ally_discord_id)
            current_rep = user_record.get("rep", 0)
            if not isinstance(current_rep, int):
                current_rep = 0
            user_record["rep"] = current_rep + 1

    report_results[thread_key] = "won" if won_fight else "lost"
    save_guild_config(bot.guild_config)

    await send_resolution_dms(
        thread.guild,
        reporter_discord_id if isinstance(reporter_discord_id, int) else None,
        ally_discord_ids,
        won_fight,
    )

    try:
        await thread.delete()
    except discord.Forbidden:
        pass


class CreateReportModal(discord.ui.Modal, title="Create AT Report"):
    def __init__(self, saved_user: dict[str, str] | None = None) -> None:
        super().__init__()
        self.saved_user = saved_user

        if saved_user is None:
            self.reporter = discord.ui.TextInput(
                label="Your username or profile url",
                placeholder="Make sure to keep joins on",
                max_length=200,
            )
        else:
            self.reporter = None
            self.saved_user_note = discord.ui.TextDisplay(
                f"Using user `{saved_user['username']}`, run `/setuser` to change it."
            )
        self.teamer_one = discord.ui.TextInput(
            label="Teamer 1",
            placeholder="Username or Roblox profile URL",
            max_length=200,
        )
        self.teamer_two = discord.ui.TextInput(
            label="Teamer 2",
            placeholder="Username or Roblox profile URL",
            max_length=200,
        )
        self.teamer_three = discord.ui.TextInput(
            label="Teamer 3",
            placeholder="Username or Roblox profile URL (optional)",
            required=False,
            max_length=200,
        )
        self.addteamer_note = discord.ui.TextDisplay(
            "You can add more teamers in the post."
        )

        if self.reporter is not None:
            self.add_item(self.reporter)
        else:
            self.add_item(self.saved_user_note)
        self.add_item(self.teamer_one)
        self.add_item(self.teamer_two)
        self.add_item(self.teamer_three)
        self.add_item(self.addteamer_note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        bot = interaction.client
        if not isinstance(bot, ATBot):
            await interaction.response.send_message(
                "The bot client is not ready yet.",
                ephemeral=True,
            )
            return

        guild_config = bot.guild_config.get(str(interaction.guild.id))
        if guild_config is None:
            await interaction.response.send_message(
                "No report forum is configured yet. Run `/setchannel` first.",
                ephemeral=True,
            )
            return

        forum_channel = interaction.guild.get_channel(guild_config["forum_channel_id"])
        if not isinstance(forum_channel, (discord.TextChannel, discord.ForumChannel)):
            await interaction.response.send_message(
                "The configured forum/reports channel could not be found.",
                ephemeral=True,
            )
            return

        mention_role_id = guild_config.get("mention_role_id")
        if not isinstance(mention_role_id, int):
            await interaction.response.send_message(
                "No mention role is configured yet. Run `/setup` first.",
                ephemeral=True,
            )
            return

        reporter_value = (
            self.reporter.value.strip()
            if self.reporter is not None
            else self.saved_user["value"]
        )
        player_entries = [
            self.teamer_one.value.strip(),
            self.teamer_two.value.strip(),
        ]
        optional_teamer = self.teamer_three.value.strip()
        if optional_teamer:
            player_entries.append(optional_teamer)

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            reporter = await bot.roblox.resolve_user(reporter_value)
            teamers = [
                await bot.roblox.resolve_user(entry)
                for entry in player_entries
            ]
        except RobloxAPIError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return

        allies: list[RobloxUser] = []
        title = build_report_thread_title(reporter, allies, teamers)
        embeds = build_report_embeds(
            reporter,
            interaction.user.name,
            allies,
            [],
            teamers,
        )
        join_view = ReportActionView(reporter.join_url)
        role_mention = f"<@&{mention_role_id}>"

        try:
            if isinstance(forum_channel, discord.ForumChannel):
                thread_with_message = await forum_channel.create_thread(
                    name=title,
                    content=role_mention,
                    embeds=embeds[:10],
                    view=join_view,
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
                target_name = thread_with_message.thread.mention
                report_thread_id = thread_with_message.thread.id
            else:
                await forum_channel.send(
                    content=role_mention,
                    embeds=embeds[:10],
                    view=join_view,
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
                target_name = forum_channel.mention
                report_thread_id = None
        except discord.Forbidden:
            await interaction.followup.send(
                "I do not have permission to create the report there.",
                ephemeral=True,
            )
            return

        if report_thread_id is not None:
            guild_state = get_guild_state(bot, interaction.guild.id)
            guild_state["reporter_ids"][str(report_thread_id)] = interaction.user.id
            guild_state.setdefault("reporter_usernames", {})[str(report_thread_id)] = (
                interaction.user.name
            )
            guild_state.setdefault("ally_user_ids", {})[str(report_thread_id)] = []
            guild_state.setdefault("ally_usernames", {})[str(report_thread_id)] = []
            save_guild_config(bot.guild_config)

        await interaction.followup.send(
            embed=discord.Embed(
                title="Report Created",
                description=f"Your report was posted in {target_name}.",
                color=EMBED_COLOR,
            ),
            ephemeral=True,
        )


class CreateReportView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Create Teaming Report",
        style=discord.ButtonStyle.primary,
        emoji="📝",
        custom_id="report:create",
    )
    async def create_report(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        bot_client = interaction.client
        if not isinstance(bot_client, ATBot):
            await interaction.response.send_message(
                "The bot client is not ready yet.",
                ephemeral=True,
            )
            return

        guild_state = get_guild_state(bot_client, interaction.guild.id)
        saved_user = get_saved_user_entry(guild_state, interaction.user.id)
        await interaction.response.send_modal(CreateReportModal(saved_user))


class AddTeamerModal(discord.ui.Modal, title="Add Teamers"):
    def __init__(self) -> None:
        super().__init__()

        self.teamer_one = discord.ui.TextInput(
            label="Teamer 1",
            placeholder="Username or Roblox profile URL",
            max_length=200,
        )
        self.teamer_two = discord.ui.TextInput(
            label="Teamer 2",
            placeholder="Username or Roblox profile URL (optional)",
            required=False,
            max_length=200,
        )
        self.teamer_three = discord.ui.TextInput(
            label="Teamer 3",
            placeholder="Username or Roblox profile URL (optional)",
            required=False,
            max_length=200,
        )
        self.teamer_four = discord.ui.TextInput(
            label="Teamer 4",
            placeholder="Username or Roblox profile URL (optional)",
            required=False,
            max_length=200,
        )

        self.add_item(self.teamer_one)
        self.add_item(self.teamer_two)
        self.add_item(self.teamer_three)
        self.add_item(self.teamer_four)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        bot_client = interaction.client
        if not isinstance(bot_client, ATBot):
            await interaction.response.send_message(
                "The bot client is not ready yet.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Use this inside the report thread.",
                ephemeral=True,
            )
            return

        raw_players = [self.teamer_one.value.strip()]
        optional_players = [
            self.teamer_two.value.strip(),
            self.teamer_three.value.strip(),
            self.teamer_four.value.strip(),
        ]
        raw_players.extend(player for player in optional_players if player)

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            _, _, _, added_teamers = await append_teamers_to_report(
                bot_client,
                interaction.channel,
                raw_players,
            )
        except (RobloxAPIError, ValueError, discord.NotFound) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.followup.send(
                "I do not have permission to edit this report.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=discord.Embed(
                title="Teamers Added",
                description="Added: " + ", ".join(teamer.label for teamer in added_teamers),
                color=EMBED_COLOR,
            ),
            ephemeral=True,
        )


class AddTeamerButton(discord.ui.Button["ReportActionView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Add teamer",
            style=discord.ButtonStyle.secondary,
            custom_id="report:addteamer",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AddTeamerModal())


class AddYourselfModal(discord.ui.Modal, title="Add Yourself"):
    def __init__(self) -> None:
        super().__init__()
        self.username = discord.ui.TextInput(
            label="Your username or profile url",
            placeholder="Username or Roblox profile URL",
            max_length=200,
        )
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        bot_client = interaction.client
        if not isinstance(bot_client, ATBot):
            await interaction.response.send_message(
                "The bot client is not ready yet.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Use this inside the report thread.",
                ephemeral=True,
            )
            return

        guild_state = get_guild_state(bot_client, interaction.guild.id)
        reporter_ids = guild_state["reporter_ids"]
        if reporter_ids.get(str(interaction.channel.id)) == interaction.user.id:
            await interaction.response.send_message(
                "The reporter cannot use Add yourself on their own report.",
                ephemeral=True,
            )
            return

        raw_player = self.username.value.strip()
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            resolved_user = await bot_client.roblox.resolve_user(raw_player)
            set_saved_user_entry(guild_state, interaction.user.id, resolved_user)
            save_guild_config(bot_client.guild_config)
            _, _, _, added_allies = await append_allies_to_report(
                bot_client,
                interaction.channel,
                [resolved_user.profile_url],
                interaction.user.id,
            )
        except (RobloxAPIError, ValueError, discord.NotFound) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.followup.send(
                "I do not have permission to edit this report.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=discord.Embed(
                title="Added Yourself",
                description="Added: " + ", ".join(ally.label for ally in added_allies),
                color=ALLY_EMBED_COLOR,
            ),
            ephemeral=True,
        )


class AddYourselfButton(discord.ui.Button["ReportActionView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Add yourself",
            style=discord.ButtonStyle.success,
            custom_id="report:addyourself",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Use this inside the report thread.",
                ephemeral=True,
            )
            return

        bot_client = interaction.client
        if not isinstance(bot_client, ATBot):
            await interaction.response.send_message(
                "The bot client is not ready yet.",
                ephemeral=True,
            )
            return

        guild_state = get_guild_state(bot_client, interaction.guild.id)
        reporter_ids = guild_state["reporter_ids"]
        if reporter_ids.get(str(interaction.channel.id)) == interaction.user.id:
            await interaction.response.send_message(
                "The reporter cannot use Add yourself on their own report.",
                ephemeral=True,
            )
            return

        removed_rep_user_ids = guild_state.setdefault("removed_rep_user_ids", {}).get(
            str(interaction.channel.id),
            [],
        )
        if interaction.user.id in removed_rep_user_ids:
            await interaction.response.send_message(
                "You cannot join this report again after having rep removed.",
                ephemeral=True,
            )
            return

        remembered_user = get_saved_user_entry(guild_state, interaction.user.id)
        if not remembered_user:
            await interaction.response.send_modal(AddYourselfModal())
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            _, _, _, added_allies = await append_allies_to_report(
                bot_client,
                interaction.channel,
                [remembered_user["value"]],
                interaction.user.id,
            )
        except (RobloxAPIError, ValueError, discord.NotFound) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.followup.send(
                "I do not have permission to edit this report.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=discord.Embed(
                title="Added Yourself",
                description="Added: " + ", ".join(ally.label for ally in added_allies),
                color=ALLY_EMBED_COLOR,
            ),
            ephemeral=True,
        )


class CloseDecisionView(discord.ui.View):
    def __init__(self, reporter_discord_id: int) -> None:
        super().__init__(timeout=600)
        self.reporter_discord_id = reporter_discord_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.reporter_discord_id:
            return True

        await interaction.response.send_message(
            "Only the reporter can answer this.",
            ephemeral=True,
        )
        return False

    async def finish_resolution(
        self,
        interaction: discord.Interaction,
        won_fight: bool,
    ) -> None:
        bot_client = interaction.client
        if not isinstance(bot_client, ATBot):
            await interaction.response.send_message(
                "The bot client is not ready yet.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "This can only be used inside a report thread.",
                ephemeral=True,
            )
            return

        guild_state = get_guild_state(bot_client, interaction.guild.id)
        report_results = guild_state["report_results"]
        thread_key = str(interaction.channel.id)
        if report_results.get(thread_key):
            await interaction.response.send_message(
                "This report has already been closed.",
                ephemeral=True,
            )
            return

        for child in self.children:
            child.disabled = True

        outcome_text = "won" if won_fight else "lost"
        color = ALLY_EMBED_COLOR if won_fight else TEAMER_EMBED_COLOR
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Fight Closed",
                description=f"The reporter marked this fight as {outcome_text}.",
                color=color,
            ),
            view=self,
        )

        await resolve_report(bot_client, interaction.channel, won_fight)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def confirm_yes(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.finish_resolution(interaction, True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def confirm_no(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.finish_resolution(interaction, False)


class CloseReportButton(discord.ui.Button["ReportActionView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Close post",
            style=discord.ButtonStyle.danger,
            custom_id="report:close",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Use this inside the report thread.",
                ephemeral=True,
            )
            return

        bot_client = interaction.client
        if not isinstance(bot_client, ATBot):
            await interaction.response.send_message(
                "The bot client is not ready yet.",
                ephemeral=True,
            )
            return

        guild_state = get_guild_state(bot_client, interaction.guild.id)
        reporter_ids = guild_state["reporter_ids"]
        report_results = guild_state["report_results"]
        thread_key = str(interaction.channel.id)
        reporter_discord_id = reporter_ids.get(thread_key)

        if report_results.get(thread_key):
            await interaction.response.send_message(
                "This report has already been closed.",
                ephemeral=True,
            )
            return

        if reporter_discord_id != interaction.user.id:
            await interaction.response.send_message(
                "Only the reporter can close this report.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Close Report",
                description="Have you won the fight?",
                color=EMBED_COLOR,
            ),
            view=CloseDecisionView(interaction.user.id),
        )


class ReportActionView(discord.ui.View):
    def __init__(self, join_url: str) -> None:
        super().__init__(timeout=None)
        self.add_item(AddYourselfButton())
        self.add_item(AddTeamerButton())
        self.add_item(
            discord.ui.Button(
                label="Join user",
                style=discord.ButtonStyle.link,
                url=join_url,
            )
        )
        self.add_item(CloseReportButton())


async def get_report_message(channel: discord.abc.GuildChannel) -> discord.Message:
    if isinstance(channel, discord.Thread):
        return await channel.fetch_message(channel.id)
    raise ValueError("This command must be used inside a report thread.")


class ATBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True

        super().__init__(
            command_prefix="!",
            intents=intents,
        )
        self.http_session: aiohttp.ClientSession | None = None
        self.roblox: RobloxClient
        self.guild_config = load_guild_config()

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        self.roblox = RobloxClient(self.http_session)
        self.add_view(CreateReportView())
        self.add_view(ReportActionView("https://www.roblox.com"))
        synced = await self.tree.sync()
        logger.info("Synced %s application command(s).", len(synced))

    async def close(self) -> None:
        if self.http_session is not None and not self.http_session.closed:
            await self.http_session.close()
        await super().close()


bot = ATBot()


@bot.event
async def on_ready() -> None:
    if bot.user is None:
        return

    logger.info("Logged in as %s (%s)", bot.user, bot.user.id)


@bot.tree.command(
    name="setup",
    description="Set up the AT report panel, report destination, and ping role.",
)
@app_commands.describe(
    report_channel="The channel where the AT report panel should be posted.",
    forum_channel="The forum or reports channel that reports will go into.",
    role="The role that will be mentioned on each report post.",
)
async def setup(
    interaction: discord.Interaction,
    report_channel: discord.TextChannel,
    forum_channel: discord.TextChannel | discord.ForumChannel,
    role: discord.Role,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not has_setup_permissions(
        interaction.user
    ):
        await interaction.response.send_message(
            "You do not have permission to use `/setup`.",
            ephemeral=True,
        )
        return

    bot_client = interaction.client
    if not isinstance(bot_client, ATBot):
        await interaction.response.send_message(
            "The bot client is not ready yet.",
            ephemeral=True,
        )
        return

    bot_client.guild_config[str(interaction.guild.id)] = {
        "report_channel_id": report_channel.id,
        "forum_channel_id": forum_channel.id,
        "mention_role_id": role.id,
    }
    save_guild_config(bot_client.guild_config)

    await report_channel.send(
        embed=build_panel_embed(forum_channel.mention),
        view=CreateReportView(),
    )

    await interaction.response.send_message(
        embed=discord.Embed(
            title="Panel Posted",
            description=(
                f"The AT report panel has been posted in {report_channel.mention}.\n"
                f"Reports will point people to {forum_channel.mention}.\n"
                f"Each report will mention {role.mention}."
            ),
            color=EMBED_COLOR,
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="setuser",
    description="Set your default Roblox username or profile URL for report actions.",
)
@app_commands.describe(
    user="Your Roblox username or profile URL.",
)
async def setuser(
    interaction: discord.Interaction,
    user: str,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    bot_client = interaction.client
    if not isinstance(bot_client, ATBot):
        await interaction.response.send_message(
            "The bot client is not ready yet.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        roblox_user = await bot_client.roblox.resolve_user(user)
    except RobloxAPIError as error:
        await interaction.followup.send(str(error), ephemeral=True)
        return

    guild_state = get_guild_state(bot_client, interaction.guild.id)
    set_saved_user_entry(guild_state, interaction.user.id, roblox_user)
    save_guild_config(bot_client.guild_config)

    await interaction.followup.send(
        embed=discord.Embed(
            title="Default User Saved",
            description=(
                "Your default Roblox user is now set to "
                f"[{roblox_user.label}]({roblox_user.profile_url})."
            ),
            color=ALLY_EMBED_COLOR,
        ),
        ephemeral=True,
    )


async def removerep_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild is None or not isinstance(interaction.channel, discord.Thread):
        return []

    bot_client = interaction.client
    if not isinstance(bot_client, ATBot):
        return []

    guild_state = get_guild_state(bot_client, interaction.guild.id)
    reporter_discord_id = guild_state["reporter_ids"].get(str(interaction.channel.id))
    if reporter_discord_id != interaction.user.id:
        return []

    ally_ids = guild_state.setdefault("ally_user_ids", {}).get(str(interaction.channel.id), [])
    removed_ids = set(guild_state.setdefault("removed_rep_user_ids", {}).get(str(interaction.channel.id), []))
    results: list[app_commands.Choice[str]] = []
    lowered_current = current.lower()

    for ally_id in ally_ids:
        if not isinstance(ally_id, int) or ally_id in removed_ids:
            continue

        member = interaction.guild.get_member(ally_id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(ally_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue

        label = member.name
        if lowered_current and lowered_current not in label.lower():
            continue

        results.append(app_commands.Choice(name=label[:100], value=str(ally_id)))
        if len(results) >= 25:
            break

    return results


@app_commands.autocomplete(player=removerep_autocomplete)
@bot.tree.command(
    name="removerep",
    description="Remove rep from an ally in the current report and remove them from the list.",
)
@app_commands.describe(
    player="Choose the ally from this report.",
)
async def removerep(
    interaction: discord.Interaction,
    player: str,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    bot_client = interaction.client
    if not isinstance(bot_client, ATBot):
        await interaction.response.send_message(
            "The bot client is not ready yet.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message(
            "Run this command inside the report thread.",
            ephemeral=True,
        )
        return

    guild_state = get_guild_state(bot_client, interaction.guild.id)
    reporter_discord_id = guild_state["reporter_ids"].get(str(interaction.channel.id))
    if reporter_discord_id != interaction.user.id:
        await interaction.response.send_message(
            "Only the reporter can use this command.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        _, removed_ally = await remove_ally_from_report(
            bot_client,
            interaction.channel,
            int(player),
            interaction.user.id,
        )
    except ValueError as error:
        await interaction.followup.send(str(error), ephemeral=True)
        return
    except discord.Forbidden:
        await interaction.followup.send(
            "I do not have permission to edit this report.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        embed=discord.Embed(
            title="Rep Removed",
            description=f"Removed {removed_ally.label} from this report and took away 1 rep.",
            color=TEAMER_EMBED_COLOR,
        ),
        ephemeral=True,
    )


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is missing from the .env file.")

    bot.run(token)


if __name__ == "__main__":
    main()
