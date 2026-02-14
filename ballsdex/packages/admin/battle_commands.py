import io
import logging
import time
from typing import TYPE_CHECKING

import discord
from asgiref.sync import sync_to_async
from discord import app_commands
from discord.ext import commands

from ballsdex.core.utils.formatting import pagify
from ballsdex.core.dev import send_interactive
from bd_models.models import BallInstance, BattleRecord, Player

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.admin.battle")

# in-memory battle sessions: session_id -> dict
_battle_sessions: dict[str, dict] = {}


def _new_session_id() -> str:
    return hex(int(time.time() * 1000))[-8:]


async def _get_player(user: discord.User) -> "Player | None":
    return await sync_to_async(Player.objects.filter(discord_id=user.id).first)()


async def _get_instance_display(instance_id: int) -> str:
    try:
        inst = await sync_to_async(BallInstance.objects.get)(pk=instance_id)
        return inst.short_description()
    except Exception:
        return f"#{instance_id} (not found)"


class BattleCog(commands.Cog):
    """Battle commands"""

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot

    battle = app_commands.Group(name="battle", description="Battle commands")

    @battle.command(name="start", description="Start a battle session against another player")
    @app_commands.describe(limit="Max countryballs per player (1-3)", opponent="Player to fight")
    async def battle_start(self, interaction: discord.Interaction, limit: int = 3, opponent: discord.Member | None = None):
        if limit < 1 or limit > 3:
            await interaction.response.send_message("Limit must be between 1 and 3.", ephemeral=True)
            return

        if opponent is None:
            await interaction.response.send_message("You must specify an opponent.", ephemeral=True)
            return

        session_id = _new_session_id()
        _battle_sessions[session_id] = {
            "host": interaction.user.id,
            "opponent": opponent.id,
            "limit": limit,
            "team_a": [],
            "team_b": [],
            "confirmed": {interaction.user.id: False, opponent.id: False},
            "channel": interaction.channel_id,
        }

        await interaction.response.send_message(
            f"Battle session `{session_id}` started between {interaction.user.mention} and {opponent.mention}.\n"
            f"Each player may add up to {limit} countryballs with `/battle add {session_id} <instance_id>`.",
            ephemeral=False,
        )

    @battle.command(name="add", description="Add a countryball instance to an existing battle session")
    @app_commands.describe(session="Battle session id", instance_id="BallInstance id to add")
    async def battle_add(self, interaction: discord.Interaction, session: str, instance_id: int):
        sess = _battle_sessions.get(session)
        if not sess:
            await interaction.response.send_message("Session not found.", ephemeral=True)
            return

        uid = interaction.user.id
        if uid not in (sess["host"], sess["opponent"]):
            await interaction.response.send_message("You are not part of this session.", ephemeral=True)
            return

        # fetch BallInstance and check ownership
        try:
            inst = await sync_to_async(BallInstance.objects.get)(pk=instance_id)
        except Exception:
            await interaction.response.send_message("BallInstance not found.", ephemeral=True)
            return

        player = await _get_player(interaction.user)
        if not player or inst.player.discord_id != interaction.user.id:
            await interaction.response.send_message("You do not own that countryball instance.", ephemeral=True)
            return

        # determine which team to add to
        if uid == sess["host"]:
            team = sess["team_a"]
        else:
            team = sess["team_b"]

        if len(team) >= sess["limit"]:
            await interaction.response.send_message(f"Your team already has the maximum of {sess['limit']} balls.", ephemeral=True)
            return

        if instance_id in sess["team_a"] or instance_id in sess["team_b"]:
            await interaction.response.send_message("This instance is already added to the session.", ephemeral=True)
            return

        team.append(instance_id)
        sess["confirmed"][sess["host"]] = False
        sess["confirmed"][sess["opponent"]] = False

        await interaction.response.send_message(f"Added instance #{instance_id} to your team in session `{session}`.", ephemeral=True)

    @battle.command(name="confirm", description="Confirm your readiness for the battle session")
    @app_commands.describe(session="Battle session id")
    async def battle_confirm(self, interaction: discord.Interaction, session: str):
        sess = _battle_sessions.get(session)
        if not sess:
            await interaction.response.send_message("Session not found.", ephemeral=True)
            return

        uid = interaction.user.id
        if uid not in (sess["host"], sess["opponent"]):
            await interaction.response.send_message("You are not part of this session.", ephemeral=True)
            return

        # ensure both players have at least one ball
        if uid == sess["host"] and not sess["team_a"]:
            await interaction.response.send_message("You must add at least one countryball before confirming.", ephemeral=True)
            return
        if uid == sess["opponent"] and not sess["team_b"]:
            await interaction.response.send_message("You must add at least one countryball before confirming.", ephemeral=True)
            return

        sess["confirmed"][uid] = True
        await interaction.response.send_message("You have confirmed. Waiting for the other player...", ephemeral=True)

        # if both confirmed, run simulation
        if all(sess["confirmed"].values()):
            # load instances
            a_ids = sess["team_a"]
            b_ids = sess["team_b"]
            a_list = [await sync_to_async(BallInstance.objects.get)(pk=i) for i in a_ids]
            b_list = [await sync_to_async(BallInstance.objects.get)(pk=i) for i in b_ids]

            from ballsdex.core.battle import TeamBattle

            battle = TeamBattle(a_list, b_list)
            logs = battle.run()

            text = "\n".join(logs)

            # save BattleRecord
            try:
                await sync_to_async(BattleRecord.objects.create)(
                    team_a={"ids": a_ids},
                    team_b={"ids": b_ids},
                    log=text,
                    winner=(
                        "A"
                        if any(p.current_hp > 0 for p in battle.team_a)
                        and not any(p.current_hp > 0 for p in battle.team_b)
                        else (
                            "B"
                            if any(p.current_hp > 0 for p in battle.team_b)
                            and not any(p.current_hp > 0 for p in battle.team_a)
                            else "draw"
                        )
                    ),
                )
            except Exception:
                log.exception("Failed to persist BattleRecord")

            # send as txt file to channel
            bio = io.BytesIO()
            bio.write(text.encode("utf-8"))
            bio.seek(0)
            filename = f"battle_{session}.txt"

            chan = interaction.channel
            await chan.send(file=discord.File(bio, filename=filename))
            # cleanup session
            del _battle_sessions[session]

    @commands.command()
    async def battle(self, ctx: commands.Context, *ids: int):
        """
        Simulate a battle between teams of BallInstance ids.
        Usage:
        - `!battle A B` (1v1)
        - `!battle A1 A2 B1 B2` (2v2)
        - `!battle A1 A2 A3 B1 B2 B3` (3v3)
        """
        if len(ids) not in (2, 4, 6):
            await ctx.send("Provide 2, 4 or 6 BallInstance ids. Example: `!battle 10 11 20 21` for 2v2.")
            return

        half = len(ids) // 2
        a_ids = ids[:half]
        b_ids = ids[half:]

        try:
            a_list = [await sync_to_async(BallInstance.objects.get)(pk=i) for i in a_ids]
            b_list = [await sync_to_async(BallInstance.objects.get)(pk=i) for i in b_ids]
        except Exception:
            await ctx.send("One or more BallInstance ids not found.")
            return

        from ballsdex.core.battle import TeamBattle

        battle = TeamBattle(a_list, b_list)
        logs = battle.run()

        text = "\n".join(logs)
        pages = pagify(text, delims=["\n\n", "\n"], priority=True)
        await send_interactive(ctx, pages, block=None)

