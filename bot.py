from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from roblox_api import RobloxAPIError, RobloxClient, RobloxUser


load_dotenv()

EMBED_COLOR = discord.Color.from_rgb(128, 0, 255)
ALLY_EMBED_COLOR = discord.Color.from_rgb(57, 255, 20)
TEAMER_EMBED_COLOR = discord.Color.red()
STAFF_OVERRIDE_ROLE_ID = 1496291137265078364
TEST_MODE = "--testing" in sys.argv
CONFIG_PATH = Path("guild_config_test.json" if TEST_MODE else "guild_config.json")
AUTO_CLOSE_TRIGGER_SECONDS = 3600
AUTO_CLOSE_RESPONSE_SECONDS = 60


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
        normalized: dict[str, dict[str, Any]] = {}
        for guild_id, guild_state in data.items():
            if not isinstance(guild_id, str):
                continue

            if not isinstance(guild_state, dict):
                guild_state = {}

            guild_state.setdefault("reporter_ids", {})
            guild_state.setdefault("reporter_usernames", {})
            guild_state.setdefault("report_results", {})
            guild_state.setdefault("report_notification_messages", {})
            guild_state.setdefault("auto_close_prompts", {})
            guild_state.setdefault("ally_user_ids", {})
            guild_state.setdefault("ally_usernames", {})
            guild_state.setdefault("removed_rep_user_ids", {})
            guild_state.setdefault("user_memory", {})

            user_memory = guild_state.get("user_memory", {})
            if isinstance(user_memory, dict):
                for discord_user_id in list(user_memory.keys()):
                    try:
                        get_user_memory_record(guild_state, int(discord_user_id))
                    except ValueError:
                        continue

            normalized[guild_id] = guild_state

        return normalized
    return {}


def save_guild_config(config: dict[str, dict[str, Any]]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=2, sort_keys=True)


def get_guild_state(bot: "ATBot", guild_id: int) -> dict[str, Any]:
    state = bot.guild_config.setdefault(str(guild_id), {})
    state.setdefault("reporter_ids", {})
    state.setdefault("reporter_usernames", {})
    state.setdefault("report_results", {})
    state.setdefault("report_notification_messages", {})
    state.setdefault("auto_close_prompts", {})
    state.setdefault("ally_user_ids", {})
    state.setdefault("ally_usernames", {})
    state.setdefault("removed_rep_user_ids", {})
    state.setdefault("user_memory", {})
    return state


def get_user_memory_record(guild_state: dict[str, Any], discord_user_id: int) -> dict[str, Any]:
    key = str(discord_user_id)
    entry = guild_state["user_memory"].get(key)

    if isinstance(entry, dict):
        entry.setdefault("rep", 0)
        entry.setdefault("wins", 0)
        entry.setdefault("losses", 0)
        guild_state["user_memory"][key] = entry
        return entry

    if isinstance(entry, str):
        upgraded = {
            "value": entry,
            "username": entry,
            "rep": 0,
            "wins": 0,
            "losses": 0,
        }
        guild_state["user_memory"][key] = upgraded
        return upgraded

    created = {"rep": 0, "wins": 0, "losses": 0}
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


def find_linked_discord_user_id(
    guild_state: dict[str, Any],
    roblox_user: RobloxUser,
) -> int | None:
    user_memory = guild_state.get("user_memory", {})
    if not isinstance(user_memory, dict):
        return None

    for discord_user_id, entry in user_memory.items():
        if not isinstance(entry, dict):
            continue

        linked_value = entry.get("value")
        if linked_value == roblox_user.profile_url:
            try:
                return int(discord_user_id)
            except ValueError:
                return None

    return None


def ensure_user_link_available(
    guild_state: dict[str, Any],
    discord_user_id: int,
    roblox_user: RobloxUser,
) -> None:
    linked_discord_user_id = find_linked_discord_user_id(guild_state, roblox_user)
    if linked_discord_user_id is None or linked_discord_user_id == discord_user_id:
        return

    raise ValueError(
        f"The Roblox account `{roblox_user.username}` is already linked to another Discord user."
    )


def build_panel_embed(forum_channel_mention: str) -> discord.Embed:
    return discord.Embed(
        title="Teamer report",
        description=(
            "Use this panel to quickly open a teaming report, bring in help, and keep "
            "everything organized in one place. When a report is created, the configured "
            f"team role will be pinged and the post will go into {forum_channel_mention}."
        ),
        color=EMBED_COLOR,
    )


def build_panel_commands_embed() -> discord.Embed:
    embed = discord.Embed(
        title="How To Use It",
        description="Public tools available after a report is created:",
        color=EMBED_COLOR,
    )
    embed.add_field(
        name="Create Teaming Report",
        value="Open a new report and list the teamers you are fighting.",
        inline=False,
    )
    embed.add_field(
        name="Add yourself",
        value="Join a report as a helper. Your saved Roblox user will be used if you already set one.",
        inline=False,
    )
    embed.add_field(
        name="Add teamer",
        value="Add more teamers to the report from inside the post.",
        inline=False,
    )
    embed.add_field(
        name="Join user",
        value="Open the reporter's Roblox link so you can get to them faster.",
        inline=False,
    )
    embed.add_field(
        name="Close post",
        value="Reporter only. Ends the report and records the outcome of the fight.",
        inline=False,
    )
    embed.add_field(
        name="/removerep",
        value="Reporter only. Remove an unhelpful ally from the report and take away 1 rep.",
        inline=False,
    )
    embed.add_field(
        name="/setuser",
        value="Save your Roblox account so you do not have to type it every time.",
        inline=False,
    )
    embed.add_field(
        name="/leaderboard",
        value="View the current helper leaderboard for the server.",
        inline=False,
    )
    return embed


def build_panel_image_embed() -> discord.Embed:
    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_image(url="attachment://ATpfp.png")
    return embed


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


def build_report_count_title(
    allies: list[RobloxUser],
    teamers: list[RobloxUser],
) -> str:
    return f"{1 + len(allies)}v{len(teamers)}"


def build_report_notification_embed(
    title: str,
    thread: discord.Thread,
) -> discord.Embed:
    return discord.Embed(
        title=title,
        color=EMBED_COLOR,
        timestamp=thread.created_at,
    )


def extract_thread_id_from_notification_message(
    guild_state: dict[str, Any],
    message: discord.Message | None,
) -> int | None:
    if message is None:
        return None

    notification_messages = guild_state.get("report_notification_messages", {})
    if not isinstance(notification_messages, dict):
        return None

    for thread_id_text, entry in notification_messages.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("message_id") == message.id:
            if thread_id_text.isdigit():
                return int(thread_id_text)
            return None

    return None


def compute_status_messages(config: dict[str, dict[str, Any]]) -> list[str]:
    total_wins = 0
    total_losses = 0
    active_helpers = 0

    for guild_state in config.values():
        if not isinstance(guild_state, dict):
            continue

        user_memory = guild_state.get("user_memory", {})
        if not isinstance(user_memory, dict):
            continue

        for user_record in user_memory.values():
            if not isinstance(user_record, dict):
                continue

            wins = user_record.get("wins", 0)
            losses = user_record.get("losses", 0)
            rep = user_record.get("rep", 0)

            if isinstance(wins, int):
                total_wins += wins
            if isinstance(losses, int):
                total_losses += losses
            if isinstance(rep, int) and rep > 1:
                active_helpers += 1

    total_fights = total_wins + total_losses
    winrate = 0.0 if total_fights == 0 else (total_wins / total_fights) * 100

    return [
        f"Won {total_wins} times against teamers",
        f"{winrate:.1f}% Winrate",
        f"{active_helpers} Active helpers",
        "My dad is GS :P",
    ]


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


def has_staff_override(member: discord.Member) -> bool:
    return any(role.id == STAFF_OVERRIDE_ROLE_ID for role in member.roles)


def can_manage_report(member: discord.Member, reporter_discord_id: int) -> bool:
    return (
        member.id == reporter_discord_id
        or has_staff_override(member)
        or member.guild_permissions.manage_threads
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


async def get_thread_by_id(
    guild: discord.Guild,
    thread_id: int,
) -> discord.Thread | None:
    thread = guild.get_thread(thread_id)
    if thread is not None:
        return thread

    channel = guild.get_channel(thread_id)
    if isinstance(channel, discord.Thread):
        return channel

    try:
        fetched_channel = await guild.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None

    if isinstance(fetched_channel, discord.Thread):
        return fetched_channel
    return None


async def sync_report_notification(
    bot: "ATBot",
    thread: discord.Thread,
    title: str,
) -> None:
    guild_state = get_guild_state(bot, thread.guild.id)
    notification_entry = guild_state.setdefault("report_notification_messages", {}).get(
        str(thread.id)
    )
    if not isinstance(notification_entry, dict):
        return

    channel_id = notification_entry.get("channel_id")
    message_id = notification_entry.get("message_id")
    if not isinstance(channel_id, int) or not isinstance(message_id, int):
        return

    channel = thread.guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        message = await channel.fetch_message(message_id)
        await message.edit(
            embed=build_report_notification_embed(title, thread),
            view=NotificationActionView(),
        )
    except discord.NotFound:
        guild_state["report_notification_messages"].pop(str(thread.id), None)
        save_guild_config(bot.guild_config)
    except discord.Forbidden:
        return


async def delete_report_notification(
    bot: "ATBot",
    thread: discord.Thread,
) -> None:
    guild_state = get_guild_state(bot, thread.guild.id)
    notification_entry = guild_state.setdefault("report_notification_messages", {}).pop(
        str(thread.id),
        None,
    )
    save_guild_config(bot.guild_config)

    if not isinstance(notification_entry, dict):
        return

    channel_id = notification_entry.get("channel_id")
    message_id = notification_entry.get("message_id")
    if not isinstance(channel_id, int) or not isinstance(message_id, int):
        return

    channel = thread.guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        message = await channel.fetch_message(message_id)
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        return


async def announce_report_notification(
    bot: "ATBot",
    thread: discord.Thread,
    reporter_discord_id: int,
    mention_role_id: int,
    notifications_channel_id: int,
    title: str,
) -> None:
    notifications_channel = thread.guild.get_channel(notifications_channel_id)
    if not isinstance(notifications_channel, discord.TextChannel):
        return

    message = await notifications_channel.send(
        content=f"<@&{mention_role_id}>, <@{reporter_discord_id}> needs help.",
        embed=build_report_notification_embed(title, thread),
        view=NotificationActionView(),
        allowed_mentions=discord.AllowedMentions(roles=True, users=True),
    )

    guild_state = get_guild_state(bot, thread.guild.id)
    guild_state.setdefault("report_notification_messages", {})[str(thread.id)] = {
        "channel_id": notifications_channel.id,
        "message_id": message.id,
    }
    save_guild_config(bot.guild_config)


async def bump_report_notification(
    bot: "ATBot",
    thread: discord.Thread,
) -> None:
    guild_state = get_guild_state(bot, thread.guild.id)
    reporter_discord_id = guild_state["reporter_ids"].get(str(thread.id))
    mention_role_id = guild_state.get("mention_role_id")
    report_channel_id = guild_state.get("report_channel_id")

    if not isinstance(reporter_discord_id, int):
        raise ValueError("This report does not have a reporter linked to it.")
    if not isinstance(mention_role_id, int):
        raise ValueError("No mention role is configured for this server.")
    if not isinstance(report_channel_id, int):
        raise ValueError("No report channel is configured for this server.")

    report_message = await get_report_message(thread)
    reporter, allies, teamers = await parse_report_message(bot, report_message)
    count_title = build_report_count_title(allies, teamers)

    await delete_report_notification(bot, thread)
    await announce_report_notification(
        bot,
        thread,
        reporter_discord_id,
        mention_role_id,
        report_channel_id,
        count_title,
    )


async def send_reporter_prompt(
    bot: "ATBot",
    thread: discord.Thread,
) -> None:
    guild_state = get_guild_state(bot, thread.guild.id)
    reporter_discord_id = guild_state["reporter_ids"].get(str(thread.id))
    if not isinstance(reporter_discord_id, int):
        return

    member = thread.guild.get_member(reporter_discord_id)
    if member is None:
        try:
            member = await thread.guild.fetch_member(reporter_discord_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    await member.send(
        "Has the fight ended? If you do not respond within 1 minute, your post will be deleted.",
        view=AutoClosePromptView(thread.guild.id, thread.id, reporter_discord_id),
    )

    guild_state.setdefault("auto_close_prompts", {})[str(thread.id)] = {
        "reporter_id": reporter_discord_id,
        "expires_at": time.time() + AUTO_CLOSE_RESPONSE_SECONDS,
    }
    save_guild_config(bot.guild_config)


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
    new_title = build_report_thread_title(reporter, allies, existing_teamers)
    await thread.edit(name=new_title)
    await sync_report_notification(
        bot,
        thread,
        build_report_count_title(allies, existing_teamers),
    )

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
    new_title = build_report_thread_title(reporter, existing_allies, teamers)
    await thread.edit(name=new_title)
    await sync_report_notification(
        bot,
        thread,
        build_report_count_title(existing_allies, teamers),
    )

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
    new_title = build_report_thread_title(reporter, existing_allies, teamers)
    await thread.edit(name=new_title)
    await sync_report_notification(
        bot,
        thread,
        build_report_count_title(existing_allies, teamers),
    )

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
    auto_close_prompts = guild_state.setdefault("auto_close_prompts", {})

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

    if isinstance(reporter_discord_id, int):
        reporter_record = get_user_memory_record(guild_state, reporter_discord_id)
        if won_fight:
            current_wins = reporter_record.get("wins", 0)
            if not isinstance(current_wins, int):
                current_wins = 0
            reporter_record["wins"] = current_wins + 1
        else:
            current_losses = reporter_record.get("losses", 0)
            if not isinstance(current_losses, int):
                current_losses = 0
            reporter_record["losses"] = current_losses + 1

    report_results[thread_key] = "won" if won_fight else "lost"
    auto_close_prompts.pop(thread_key, None)
    save_guild_config(bot.guild_config)

    await send_resolution_dms(
        thread.guild,
        reporter_discord_id if isinstance(reporter_discord_id, int) else None,
        ally_discord_ids,
        won_fight,
    )

    await delete_report_notification(bot, thread)

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

        notifications_channel_id = guild_config.get("report_channel_id")
        if not isinstance(notifications_channel_id, int):
            await interaction.response.send_message(
                "No report channel is configured yet. Run `/setup` first.",
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

        guild_state = get_guild_state(bot, interaction.guild.id)
        try:
            ensure_user_link_available(guild_state, interaction.user.id, reporter)
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return

        set_saved_user_entry(guild_state, interaction.user.id, reporter)
        save_guild_config(bot.guild_config)

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
        reporter_mention = f"<@{interaction.user.id}>"

        try:
            if isinstance(forum_channel, discord.ForumChannel):
                thread_with_message = await forum_channel.create_thread(
                    name=title,
                    content=reporter_mention,
                    embeds=embeds[:10],
                    view=join_view,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                target_name = thread_with_message.thread.mention
                report_thread_id = thread_with_message.thread.id
            else:
                await forum_channel.send(
                    content=reporter_mention,
                    embeds=embeds[:10],
                    view=join_view,
                    allowed_mentions=discord.AllowedMentions(users=True),
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
            thread = await get_thread_by_id(interaction.guild, report_thread_id)
            if thread is not None:
                await announce_report_notification(
                    bot,
                    thread,
                    interaction.user.id,
                    mention_role_id,
                    notifications_channel_id,
                    build_report_count_title(allies, teamers),
                )

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
        mention_role_id = guild_state.get("mention_role_id")
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        if not isinstance(mention_role_id, int):
            await interaction.response.send_message(
                "No report role is configured yet. Run `/setup` first.",
                ephemeral=True,
            )
            return

        if mention_role_id not in {role.id for role in interaction.user.roles}:
            await interaction.response.send_message(
                "You must have the configured anti-teaming role to create a report.",
                ephemeral=True,
            )
            return

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
            ensure_user_link_available(guild_state, interaction.user.id, resolved_user)
            set_saved_user_entry(guild_state, interaction.user.id, resolved_user)
            save_guild_config(bot_client.guild_config)
            _, _, _, added_allies = await append_allies_to_report(
                bot_client,
                interaction.channel,
                [resolved_user.profile_url],
                interaction.user.id,
            )
            await post_helper_join_message(interaction.channel, interaction.user.id)
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
            await post_helper_join_message(interaction.channel, interaction.user.id)
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


async def post_helper_join_message(
    thread: discord.Thread,
    helper_discord_id: int,
) -> None:
    await thread.send(
        content=f"<@{helper_discord_id}> has joined to help! {thread.mention}",
        allowed_mentions=discord.AllowedMentions(users=True),
    )


class JoinNotificationModal(discord.ui.Modal, title="Join Report"):
    def __init__(self, thread_id: int) -> None:
        super().__init__()
        self.thread_id = thread_id
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

        thread = await get_thread_by_id(interaction.guild, self.thread_id)
        if thread is None:
            await interaction.response.send_message(
                "I could not find the report thread for this notification.",
                ephemeral=True,
            )
            return

        guild_state = get_guild_state(bot_client, interaction.guild.id)
        reporter_ids = guild_state["reporter_ids"]
        if reporter_ids.get(str(thread.id)) == interaction.user.id:
            await interaction.response.send_message(
                "The reporter cannot use Join on their own report.",
                ephemeral=True,
            )
            return

        raw_player = self.username.value.strip()
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            resolved_user = await bot_client.roblox.resolve_user(raw_player)
            ensure_user_link_available(guild_state, interaction.user.id, resolved_user)
            set_saved_user_entry(guild_state, interaction.user.id, resolved_user)
            save_guild_config(bot_client.guild_config)
            _, _, _, added_allies = await append_allies_to_report(
                bot_client,
                thread,
                [resolved_user.profile_url],
                interaction.user.id,
            )
            await post_helper_join_message(thread, interaction.user.id)
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
                title="Joined squad",
                description=f"You joined {thread.mention}.",
                color=ALLY_EMBED_COLOR,
            ),
            ephemeral=True,
        )


class NotificationJoinButton(discord.ui.Button["NotificationActionView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Join",
            style=discord.ButtonStyle.success,
            custom_id="report:joinnotification",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
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
        thread_id = extract_thread_id_from_notification_message(
            guild_state,
            interaction.message,
        )
        if thread_id is None:
            await interaction.response.send_message(
                "I could not find the report thread for this notification.",
                ephemeral=True,
            )
            return

        thread = await get_thread_by_id(interaction.guild, thread_id)
        if thread is None:
            await interaction.response.send_message(
                "I could not find the report thread for this notification.",
                ephemeral=True,
            )
            return

        reporter_ids = guild_state["reporter_ids"]
        if reporter_ids.get(str(thread.id)) == interaction.user.id:
            await interaction.response.send_message(
                "The reporter cannot use Join on their own report.",
                ephemeral=True,
            )
            return

        remembered_user = get_saved_user_entry(guild_state, interaction.user.id)
        if not remembered_user:
            await interaction.response.send_modal(JoinNotificationModal(thread.id))
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            _, _, _, added_allies = await append_allies_to_report(
                bot_client,
                thread,
                [remembered_user["value"]],
                interaction.user.id,
            )
            await post_helper_join_message(thread, interaction.user.id)
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
                title="Joined squad",
                description=f"You joined {thread.mention}.",
                color=ALLY_EMBED_COLOR,
            ),
            ephemeral=True,
        )


class NotificationActionView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(NotificationJoinButton())


class AutoCloseOutcomeView(discord.ui.View):
    def __init__(self, guild_id: int, thread_id: int, reporter_discord_id: int) -> None:
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.thread_id = thread_id
        self.reporter_discord_id = reporter_discord_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.reporter_discord_id:
            return True
        await interaction.response.send_message(
            "Only the reporter can answer this.",
            ephemeral=True,
        )
        return False

    async def finish_outcome(
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

        guild = bot_client.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message(
                "I could not find the server for this report.",
                ephemeral=True,
            )
            return

        thread = await get_thread_by_id(guild, self.thread_id)
        if thread is None:
            await interaction.response.send_message(
                "I could not find the report thread anymore.",
                ephemeral=True,
            )
            return

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content="Thanks. The report outcome has been recorded.",
            view=self,
        )
        await resolve_report(bot_client, thread, won_fight)

    @discord.ui.button(label="Won", style=discord.ButtonStyle.success)
    async def won_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.finish_outcome(interaction, True)

    @discord.ui.button(label="Lost", style=discord.ButtonStyle.danger)
    async def lost_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.finish_outcome(interaction, False)


class AutoClosePromptView(discord.ui.View):
    def __init__(self, guild_id: int, thread_id: int, reporter_discord_id: int) -> None:
        super().__init__(timeout=AUTO_CLOSE_RESPONSE_SECONDS)
        self.guild_id = guild_id
        self.thread_id = thread_id
        self.reporter_discord_id = reporter_discord_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.reporter_discord_id:
            return True
        await interaction.response.send_message(
            "Only the reporter can answer this.",
            ephemeral=True,
        )
        return False

    async def clear_prompt(self, bot: "ATBot") -> None:
        guild_state = get_guild_state(bot, self.guild_id)
        guild_state.setdefault("auto_close_prompts", {}).pop(str(self.thread_id), None)
        save_guild_config(bot.guild_config)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        bot_client = interaction.client
        if not isinstance(bot_client, ATBot):
            await interaction.response.send_message(
                "The bot client is not ready yet.",
                ephemeral=True,
            )
            return

        await self.clear_prompt(bot_client)
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content="Did you win the fight?",
            view=AutoCloseOutcomeView(
                self.guild_id,
                self.thread_id,
                self.reporter_discord_id,
            ),
        )

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        bot_client = interaction.client
        if not isinstance(bot_client, ATBot):
            await interaction.response.send_message(
                "The bot client is not ready yet.",
                ephemeral=True,
            )
            return

        guild = bot_client.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message(
                "I could not find the server for this report.",
                ephemeral=True,
            )
            return

        thread = await get_thread_by_id(guild, self.thread_id)
        if thread is None:
            await interaction.response.send_message(
                "I could not find the report thread anymore.",
                ephemeral=True,
            )
            return

        await self.clear_prompt(bot_client)
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content="Got it. The report was bumped again.",
            view=self,
        )
        await bump_report_notification(bot_client, thread)


class CloseDecisionView(discord.ui.View):
    def __init__(self, reporter_discord_id: int) -> None:
        super().__init__(timeout=600)
        self.reporter_discord_id = reporter_discord_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and can_manage_report(
            interaction.user,
            self.reporter_discord_id,
        ):
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

        if not isinstance(interaction.user, discord.Member) or not can_manage_report(
            interaction.user,
            reporter_discord_id if isinstance(reporter_discord_id, int) else -1,
        ):
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
        self.status_index = 0

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        self.roblox = RobloxClient(self.http_session)
        self.add_view(CreateReportView())
        self.add_view(ReportActionView("https://www.roblox.com"))
        self.add_view(NotificationActionView())
        synced = await self.tree.sync()
        logger.info(
            "Synced %s application command(s) globally. Test mode: %s. Memory file: %s",
            len(synced),
            TEST_MODE,
            CONFIG_PATH,
        )
        if not self.rotate_status.is_running():
            self.rotate_status.start()
        if TEST_MODE and not self.monitor_reports.is_running():
            self.monitor_reports.start()

    async def close(self) -> None:
        if self.rotate_status.is_running():
            self.rotate_status.cancel()
        if self.monitor_reports.is_running():
            self.monitor_reports.cancel()
        if self.http_session is not None and not self.http_session.closed:
            await self.http_session.close()
        await super().close()

    @tasks.loop(seconds=5)
    async def rotate_status(self) -> None:
        if self.user is None:
            return

        status_messages = compute_status_messages(self.guild_config)
        if not status_messages:
            return

        message = status_messages[self.status_index % len(status_messages)]
        self.status_index += 1
        await self.change_presence(activity=discord.Game(name=message))

    @rotate_status.before_loop
    async def before_rotate_status(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=15)
    async def monitor_reports(self) -> None:
        now = time.time()

        for guild in self.guilds:
            guild_state = get_guild_state(self, guild.id)
            reporter_ids = guild_state["reporter_ids"]
            report_results = guild_state["report_results"]
            auto_close_prompts = guild_state.setdefault("auto_close_prompts", {})

            for thread_id_text, reporter_discord_id in list(reporter_ids.items()):
                if not thread_id_text.isdigit():
                    continue

                thread_id = int(thread_id_text)
                if thread_id_text in report_results:
                    auto_close_prompts.pop(thread_id_text, None)
                    continue

                thread = await get_thread_by_id(guild, thread_id)
                if thread is None:
                    continue

                age_seconds = (discord.utils.utcnow() - thread.created_at).total_seconds()
                prompt_entry = auto_close_prompts.get(thread_id_text)

                if prompt_entry is None:
                    if age_seconds >= AUTO_CLOSE_TRIGGER_SECONDS and isinstance(
                        reporter_discord_id, int
                    ):
                        await send_reporter_prompt(self, thread)
                    continue

                expires_at = prompt_entry.get("expires_at")
                if isinstance(expires_at, (int, float)) and now >= expires_at:
                    auto_close_prompts.pop(thread_id_text, None)
                    save_guild_config(self.guild_config)

                    if isinstance(reporter_discord_id, int):
                        member = guild.get_member(reporter_discord_id)
                        if member is None:
                            try:
                                member = await guild.fetch_member(reporter_discord_id)
                            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                                member = None
                        if member is not None:
                            try:
                                await member.send(
                                    "You did not respond in time, so the report was closed as a loss and deleted."
                                )
                            except discord.Forbidden:
                                pass

                    await resolve_report(self, thread, False)

    @monitor_reports.before_loop
    async def before_monitor_reports(self) -> None:
        await self.wait_until_ready()


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

    if not isinstance(interaction.user, discord.Member) or (
        not has_setup_permissions(interaction.user)
        and not has_staff_override(interaction.user)
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

    guild_state = get_guild_state(bot_client, interaction.guild.id)
    guild_state["report_channel_id"] = report_channel.id
    guild_state["forum_channel_id"] = forum_channel.id
    guild_state["mention_role_id"] = role.id
    save_guild_config(bot_client.guild_config)

    await report_channel.send(
        embeds=[
            build_panel_embed(forum_channel.mention),
            build_panel_commands_embed(),
            build_panel_image_embed(),
        ],
        file=discord.File("ATpfp.png", filename="ATpfp.png"),
        view=CreateReportView(),
    )

    await interaction.response.send_message(
        embed=discord.Embed(
            title="Panel Posted",
            description=(
                f"The AT report panel has been posted in {report_channel.mention}.\n"
                f"Reports will point people to {forum_channel.mention}.\n"
                f"Anti-teaming notifications will also post in {report_channel.mention}.\n"
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
    try:
        ensure_user_link_available(guild_state, interaction.user.id, roblox_user)
    except ValueError as error:
        await interaction.followup.send(str(error), ephemeral=True)
        return

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


async def resolve_member_display_name(
    guild: discord.Guild,
    discord_user_id: int,
) -> str:
    member = guild.get_member(discord_user_id)
    if member is not None:
        return member.display_name

    try:
        member = await guild.fetch_member(discord_user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return f"Unknown User ({discord_user_id})"

    return member.display_name


async def resolve_member_avatar_url(
    guild: discord.Guild,
    discord_user_id: int,
) -> str | None:
    member = guild.get_member(discord_user_id)
    if member is not None:
        return member.display_avatar.url

    try:
        member = await guild.fetch_member(discord_user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None

    return member.display_avatar.url


@bot.tree.command(
    name="leaderboard",
    description="Show the helper leaderboard for this server.",
)
async def leaderboard(interaction: discord.Interaction) -> None:
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

    guild_state = get_guild_state(bot_client, interaction.guild.id)
    user_memory = guild_state.get("user_memory", {})
    if not isinstance(user_memory, dict):
        user_memory = {}

    ranked_entries: list[tuple[int, int, str]] = []
    for discord_user_id, entry in user_memory.items():
        if not isinstance(entry, dict):
            continue

        rep = entry.get("rep", 0)
        roblox_username = entry.get("username")
        if not isinstance(rep, int) or rep < 1 or not isinstance(roblox_username, str):
            continue

        try:
            ranked_entries.append((int(discord_user_id), rep, roblox_username))
        except ValueError:
            continue

    ranked_entries.sort(key=lambda item: (-item[1], item[2].lower()))

    if not ranked_entries:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Helper Leaderboard",
                description="No helpers with 1+ rep yet.",
                color=EMBED_COLOR,
            ),
            ephemeral=False,
        )
        return

    lines: list[str] = []
    for discord_user_id, rep, roblox_username in ranked_entries:
        display_name = await resolve_member_display_name(
            interaction.guild,
            discord_user_id,
        )
        lines.append(f"{display_name} ({roblox_username}) - `{rep}` rep")

    description = "\n".join(lines)
    if len(description) > 4096:
        trimmed_lines: list[str] = []
        current_length = 0
        for line in lines:
            extra_length = len(line) + (1 if trimmed_lines else 0)
            if current_length + extra_length > 4096:
                break
            trimmed_lines.append(line)
            current_length += extra_length
        description = "\n".join(trimmed_lines)

    leaderboard_embed = discord.Embed(
        title="Helper Leaderboard",
        description=description,
        color=EMBED_COLOR,
    )

    first_place_discord_user_id = ranked_entries[0][0]
    first_place_avatar_url = await resolve_member_avatar_url(
        interaction.guild,
        first_place_discord_user_id,
    )
    if first_place_avatar_url:
        leaderboard_embed.set_thumbnail(url=first_place_avatar_url)

    await interaction.response.send_message(
        embed=leaderboard_embed,
        ephemeral=False,
    )


@bot.tree.command(
    name="bumb",
    description="Re-ping the helper role for the current report after 30 minutes.",
)
async def bumb(interaction: discord.Interaction) -> None:
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
    if not isinstance(interaction.user, discord.Member) or not can_manage_report(
        interaction.user,
        reporter_discord_id if isinstance(reporter_discord_id, int) else -1,
    ):
        await interaction.response.send_message(
            "Only the reporter can use this command.",
            ephemeral=True,
        )
        return

    age_seconds = (discord.utils.utcnow() - interaction.channel.created_at).total_seconds()
    if age_seconds < 1800:
        await interaction.response.send_message(
            "You can only use `/bumb` 30 minutes into the fight.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        await bump_report_notification(bot_client, interaction.channel)
    except ValueError as error:
        await interaction.followup.send(str(error), ephemeral=True)
        return
    except discord.Forbidden:
        await interaction.followup.send(
            "I do not have permission to bump this report.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        embed=discord.Embed(
            title="Report bumped",
            description="A new helper notification was posted.",
            color=EMBED_COLOR,
        ),
        ephemeral=True,
    )


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
    if reporter_discord_id != interaction.user.id and (
        not isinstance(interaction.user, discord.Member)
        or not has_staff_override(interaction.user)
    ):
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
    token_name = "TEST_DISCORD_TOKEN" if TEST_MODE else "DISCORD_TOKEN"
    token = os.getenv(token_name)
    if not token:
        raise RuntimeError(f"{token_name} is missing from the .env file.")

    bot.run(token)


if __name__ == "__main__":
    main()
