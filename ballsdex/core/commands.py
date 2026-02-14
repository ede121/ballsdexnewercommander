import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from asgiref.sync import sync_to_async
from discord.ext import commands
from django.db import connection
from discord.ui import View, Modal, TextInput

from ballsdex.core.dev import send_interactive
from ballsdex.core.discord import LayoutView, View
from ballsdex.core.utils.formatting import pagify
from ballsdex.core.utils.menus import Menu, TextFormatter, TextSource
from bd_models.models import Ball, BallInstance
from bd_models.models import Player as DBPlayer, BattleRecord
from settings.models import load_settings, settings

log = logging.getLogger("ballsdex.core.commands")

if TYPE_CHECKING:
    from admin_panel.logging import DequeHandler

    from .bot import BallsDexBot


class SimpleCheckView(View):
    def __init__(self, ctx: commands.Context):
        super().__init__(timeout=30)
        self.ctx = ctx
        self.value = False

    async def interaction_check(self, interaction: discord.Interaction["BallsDexBot"]) -> bool:
        return interaction.user == self.ctx.author

    @discord.ui.button(style=discord.ButtonStyle.success, emoji="\N{HEAVY CHECK MARK}\N{VARIATION SELECTOR-16}")
    async def confirm_button(self, interaction: discord.Interaction["BallsDexBot"], button: discord.ui.Button):
        await interaction.response.edit_message(content="Starting upload...", view=None)
        self.value = True
        self.stop()


class Core(commands.GroupCog):
    """
    Core commands of BallsDex bot
    """

    def __init__(self, bot: "BallsDexBot"):
        super().__init__()
        self.bot = bot
        # in-memory battle sessions: session_id -> dict
        self._battle_sessions: dict[str, dict] = {}

    def _new_session_id(self) -> str:
        return hex(int(time.time() * 1000))[-8:]

    async def _get_player(self, user: discord.User) -> DBPlayer | None:
        return await sync_to_async(DBPlayer.objects.filter(discord_id=user.id).first)()

    async def _get_instance_display(self, instance_id: int) -> str:
        try:
            inst = await sync_to_async(BallInstance.objects.get)(pk=instance_id)
            return inst.short_description()
        except Exception:
            return f"#{instance_id} (not found)"

    async def _session_message_content(self, session: str) -> str:
        sess = self._battle_sessions.get(session)
        if not sess:
            return "Session not found."

        a_lines = []
        for iid in sess["team_a"]:
            a_lines.append(await self._get_instance_display(iid))
        b_lines = []
        for iid in sess["team_b"]:
            b_lines.append(await self._get_instance_display(iid))

        host_user = f"<@{sess['host']]}" if sess.get("host") else "Host"
        opp_user = f"<@{sess['opponent']]}" if sess.get("opponent") else "Opponent"

        content = (
            f"Session `{session}` — Limit: {sess['limit']}\n"
            f"Host: <@{sess['host']}>\n"
            f"Opponent: <@{sess['opponent']}>\n\n"
            f"Team A:\n" + ("\n".join(a_lines) if a_lines else "(empty)") + "\n\n"
            f"Team B:\n" + ("\n".join(b_lines) if b_lines else "(empty)") + "\n\n"
            "Use the buttons to add your balls (opens a dialog), confirm readiness, or cancel the session."
        )
        return content


    async def _add_instance_to_session(self, sess: dict, user_id: int, instance_id: int) -> str:
        # fetch instance
        try:
            inst = await sync_to_async(BallInstance.objects.get)(pk=instance_id)
        except Exception:
            return "BallInstance not found."

        if inst.player.discord_id != user_id:
            return "You do not own that countryball instance."

        if user_id == sess["host"]:
            team = sess["team_a"]
        else:
            team = sess["team_b"]

        if len(team) >= sess["limit"]:
            return f"Your team already has the maximum of {sess['limit']} balls."

        if instance_id in sess["team_a"] or instance_id in sess["team_b"]:
            return "This instance is already added to the session."

        team.append(instance_id)
        sess["confirmed"][sess["host"]] = False
        sess["confirmed"][sess["opponent"]] = False
        return f"Added instance #{instance_id} to your team."


class AddInstanceModal(Modal):
    def __init__(self, session: str, cog: Core):
        super().__init__(title="Add Countryball")
        self.session = session
        self.cog = cog
        self.instance_id = TextInput(label="Instance ID", placeholder="Enter BallInstance id", required=True)
        self.add_item(self.instance_id)

    async def on_submit(self, interaction: discord.Interaction):
        inst_val = int(self.instance_id.value.strip())
        sess = self.cog._battle_sessions.get(self.session)
        if not sess:
            await interaction.response.send_message("Session not found.", ephemeral=True)
            return

        res = await self.cog._add_instance_to_session(sess, interaction.user.id, inst_val)
        await interaction.response.send_message(res, ephemeral=True)
        # update the session message if present
        # try to edit original message in channel
        try:
            # find message by storing message id in session (optional)
            msg_id = sess.get("message_id")
            if msg_id:
                chan = interaction.channel
                content = await self.cog._session_message_content(self.session)
                try:
                    await chan.fetch_message(msg_id)
                    await chan.send("(updated)")
                except Exception:
                    pass
        except Exception:
            pass


class BattleSessionView(View):
    def __init__(self, session: str, cog: Core):
        super().__init__(timeout=None)
        self.session = session
        self.cog = cog

    @discord.ui.button(label="Add my ball", style=discord.ButtonStyle.primary)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        sess = self.cog._battle_sessions.get(self.session)
        if not sess:
            await interaction.response.send_message("Session not found.", ephemeral=True)
            return
        if interaction.user.id not in (sess["host"], sess["opponent"]):
            await interaction.response.send_message("You are not part of this session.", ephemeral=True)
            return

        await interaction.response.send_modal(AddInstanceModal(self.session, self.cog))

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        sess = self.cog._battle_sessions.get(self.session)
        if not sess:
            await interaction.response.send_message("Session not found.", ephemeral=True)
            return
        uid = interaction.user.id
        if uid not in (sess["host"], sess["opponent"]):
            await interaction.response.send_message("You are not part of this session.", ephemeral=True)
            return

        # ensure user has at least one ball
        if uid == sess["host"] and not sess["team_a"]:
            await interaction.response.send_message("You must add at least one countryball before confirming.", ephemeral=True)
            return
        if uid == sess["opponent"] and not sess["team_b"]:
            await interaction.response.send_message("You must add at least one countryball before confirming.", ephemeral=True)
            return

        sess["confirmed"][uid] = True
        await interaction.response.send_message("You have confirmed. Waiting for the other player...", ephemeral=True)

        if all(sess["confirmed"].values()):
            # run battle same as confirm command
            a_ids = sess["team_a"]
            b_ids = sess["team_b"]
            a_list = [await sync_to_async(BallInstance.objects.get)(pk=i) for i in a_ids]
            b_list = [await sync_to_async(BallInstance.objects.get)(pk=i) for i in b_ids]

            from ballsdex.core.battle import TeamBattle

            battle = TeamBattle(a_list, b_list)
            logs = battle.run()
            text = "\n".join(logs)
            # save and send file
            try:
                await sync_to_async(BattleRecord.objects.create)(team_a={"ids": a_ids}, team_b={"ids": b_ids}, log=text, winner=("A" if any(p.current_hp>0 for p in battle.team_a) and not any(p.current_hp>0 for p in battle.team_b) else ("B" if any(p.current_hp>0 for p in battle.team_b) and not any(p.current_hp>0 for p in battle.team_a) else "draw")))
            except Exception:
                log.exception("Failed to persist BattleRecord")

            import io

            bio = io.BytesIO()
            bio.write(text.encode("utf-8"))
            bio.seek(0)
            filename = f"battle_{self.session}.txt"
            chan = interaction.channel
            await chan.send(file=discord.File(bio, filename=filename))
            # cleanup
            try:
                del self.cog._battle_sessions[self.session]
            except Exception:
                pass

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        content = await self.cog._session_message_content(self.session)
        try:
            await interaction.response.edit_message(content=content, view=self)
        except Exception:
            await interaction.response.send_message(content, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        sess = self.cog._battle_sessions.get(self.session)
        if not sess:
            await interaction.response.send_message("Session not found.", ephemeral=True)
            return
        if interaction.user.id not in (sess["host"], sess["opponent"]):
            await interaction.response.send_message("Only participants can cancel the session.", ephemeral=True)
            return
        del self.cog._battle_sessions[self.session]
        await interaction.response.send_message("Session cancelled.", ephemeral=False)

    @commands.command()
    async def ping(self, ctx: commands.Context):
        """
        Ping!
        """
        await ctx.send("Pong")

    @commands.hybrid_group(name="battle", description="Battle commands")
    async def battle_group(self, ctx: commands.Context):
        """
        Battle commands for PvP battles
        """
        await ctx.send_help(ctx.command)

    @battle_group.command(name="start", description="Start a battle session against another player")
    async def slash_battle_start(self, ctx: commands.Context, limit: int = 3, opponent: discord.Member | None = None):
        if limit < 1 or limit > 3:
            await ctx.send("Limit must be between 1 and 3.", ephemeral=True)
            return

        if opponent is None:
            await ctx.send("You must specify an opponent.", ephemeral=True)
            return

        session_id = self._new_session_id()
        self._battle_sessions[session_id] = {
            "host": ctx.author.id,
            "opponent": opponent.id,
            "limit": limit,
            "team_a": [],
            "team_b": [],
            "confirmed": {ctx.author.id: False, opponent.id: False},
            "channel": ctx.channel.id,
        }

        await ctx.send(
            f"Battle session `{session_id}` started between {ctx.author.mention} and {opponent.mention}.\n"
            f"Each player may add up to {limit} countryballs with `/battle add {session_id} <instance_id>`.",
            ephemeral=False,
        )

    @battle_group.command(name="add", description="Add a countryball instance to an existing battle session")
    async def slash_battle_add(self, ctx: commands.Context, session: str, instance_id: int):
        sess = self._battle_sessions.get(session)
        if not sess:
            await ctx.send("Session not found.", ephemeral=True)
            return

        uid = ctx.author.id
        if uid not in (sess["host"], sess["opponent"]):
            await ctx.send("You are not part of this session.", ephemeral=True)
            return

        # fetch BallInstance and check ownership
        try:
            inst = await sync_to_async(BallInstance.objects.get)(pk=instance_id)
        except Exception:
            await ctx.send("BallInstance not found.", ephemeral=True)
            return

        player = await self._get_player(ctx.author)
        if not player or inst.player.discord_id != ctx.author.id:
            await ctx.send("You do not own that countryball instance.", ephemeral=True)
            return

        # determine which team to add to
        if uid == sess["host"]:
            team = sess["team_a"]
        else:
            team = sess["team_b"]

        if len(team) >= sess["limit"]:
            await ctx.send(f"Your team already has the maximum of {sess['limit']} balls.", ephemeral=True)
            return

        if instance_id in sess["team_a"] or instance_id in sess["team_b"]:
            await ctx.send("This instance is already added to the session.", ephemeral=True)
            return

        team.append(instance_id)
        sess["confirmed"][sess["host"]] = False
        sess["confirmed"][sess["opponent"]] = False

        await ctx.send(f"Added instance #{instance_id} to your team in session `{session}`.", ephemeral=True)

    @battle_group.command(name="confirm", description="Confirm your readiness for the battle session")
    async def slash_battle_confirm(self, ctx: commands.Context, session: str):
        sess = self._battle_sessions.get(session)
        if not sess:
            await ctx.send("Session not found.", ephemeral=True)
            return

        uid = ctx.author.id
        if uid not in (sess["host"], sess["opponent"]):
            await ctx.send("You are not part of this session.", ephemeral=True)
            return

        # ensure both players have at least one ball
        if uid == sess["host"] and not sess["team_a"]:
            await ctx.send("You must add at least one countryball before confirming.", ephemeral=True)
            return
        if uid == sess["opponent"] and not sess["team_b"]:
            await ctx.send("You must add at least one countryball before confirming.", ephemeral=True)
            return

        sess["confirmed"][uid] = True
        await ctx.send("You have confirmed. Waiting for the other player...", ephemeral=True)

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
                await sync_to_async(BattleRecord.objects.create)(team_a={"ids": a_ids}, team_b={"ids": b_ids}, log=text, winner=("A" if any(p.current_hp>0 for p in battle.team_a) and not any(p.current_hp>0 for p in battle.team_b) else ("B" if any(p.current_hp>0 for p in battle.team_b) and not any(p.current_hp>0 for p in battle.team_a) else "draw")))
            except Exception:
                log.exception("Failed to persist BattleRecord")

            # send as txt file to channel
            import io

            bio = io.BytesIO()
            bio.write(text.encode("utf-8"))
            bio.seek(0)
            filename = f"battle_{session}.txt"

            chan = ctx.channel
            await chan.send(file=discord.File(bio, filename=filename))
            # cleanup session
            del self._battle_sessions[session]

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

    @commands.command()
    @commands.is_owner()
    async def reloadtree(self, ctx: commands.Context):
        """
        Sync the application commands with Discord
        """
        await self.bot.tree.sync()
        await ctx.send("Application commands tree reloaded.")

    async def reload_package(self, package: str, *, with_prefix=False):
        try:
            try:
                await self.bot.reload_extension(package)
            except commands.ExtensionNotLoaded:
                await self.bot.load_extension(package)
        except commands.ExtensionNotFound:
            if not with_prefix:
                await self.reload_package("ballsdex.packages." + package, with_prefix=True)
                return
            raise

    @commands.command()
    @commands.is_owner()
    async def reload(self, ctx: commands.Context, package: str):
        """
        Reload an extension
        """
        try:
            await self.reload_package(package)
        except commands.ExtensionNotFound:
            await ctx.send("Extension not found.")
        except Exception:
            await ctx.send("Failed to reload extension.")
            log.error(f"Failed to reload extension {package}", exc_info=True)
        else:
            await ctx.send("Extension reloaded.")

    @commands.command()
    @commands.is_owner()
    async def reloadconf(self, ctx: commands.Context):
        """
        Reload the config file
        """

        await sync_to_async(load_settings)()
        await ctx.message.reply("Config values have been updated. Some changes may require a restart.")

    @commands.command()
    @commands.is_owner()
    async def reloadcache(self, ctx: commands.Context):
        """
        Reload the cache of database models.

        This is needed each time the database is updated, otherwise changes won't reflect until
        next start.
        """
        await self.bot.load_cache()
        await ctx.message.add_reaction("✅")

    @commands.command()
    @commands.is_owner()
    async def analyzedb(self, ctx: commands.Context):
        """
        Analyze the database. This refreshes the counts displayed by the `/about` command.
        """

        # pleading for this https://github.com/django/django/pull/18408
        @sync_to_async
        def wrapper():
            with connection.cursor() as cursor:
                t1 = time.time()
                cursor.execute("ANALYZE")
                t2 = time.time()
            return t2 - t1

        delta = await wrapper()
        await ctx.send(f"Analyzed database in {round(delta * 1000)}ms.")

    @commands.command()
    @commands.is_owner()
    async def migrateemotes(self, ctx: commands.Context):
        """
        Upload all guild emojis used by the bot to application emojis.

        The emoji IDs of the countryballs are updated afterwards.
        This does not delete guild emojis after they were migrated.
        """
        balls = Ball.objects.all()
        if not await balls.aexists():
            await ctx.send(f"No {settings.plural_collectible_name} found.")
            return

        application_emojis = set(x.name for x in await self.bot.fetch_application_emojis())

        not_found: set[Ball] = set()
        already_uploaded: list[tuple[Ball, discord.Emoji]] = []
        matching_name: list[tuple[Ball, discord.Emoji]] = []
        to_upload: list[tuple[Ball, discord.Emoji]] = []

        async for ball in balls:
            emote = self.bot.get_emoji(ball.emoji_id)
            if not emote:
                not_found.add(ball)
            elif emote.is_application_owned():
                already_uploaded.append((ball, emote))
            elif emote.name in application_emojis:
                matching_name.append((ball, emote))
            else:
                to_upload.append((ball, emote))

        if len(already_uploaded) == len(balls):
            await ctx.send(f"All of your {settings.plural_collectible_name} already use application emojis.")
            return
        if len(to_upload) + len(application_emojis) > 2000:
            await ctx.send(
                f"{len(to_upload)} emojis are available for migration, but this would "
                f"result in {len(to_upload) + len(application_emojis)} total application emojis, "
                "which is above the limit (2000)."
            )
            return

        text = ""
        if not_found:
            not_found_str = ", ".join(f"{x.country} ({x.emoji_id})" for x in not_found)
            text += f"### {len(not_found)} emojis not found\n{not_found_str}\n"
        if matching_name:
            matching_name_str = ", ".join(f"{x[1]} {x[0].country}" for x in matching_name)
            text += f"### {len(matching_name)} emojis with conflicting names\n{matching_name_str}\n"
        if already_uploaded:
            already_uploaded_str = ", ".join(f"{x[1]} {x[0].country}" for x in already_uploaded)
            text += f"### {len(already_uploaded)} emojis are already application emojis\n{already_uploaded_str}\n"
        if to_upload:
            to_upload_str = ", ".join(f"{x[1]} {x[0].country}" for x in to_upload)
            text += f"## {len(to_upload)} emojis to migrate\n{to_upload_str}"
        else:
            text += "\n**No emojis can be migrated at this time.**"

        pages = pagify(text, delims=["###", "\n\n", "\n"], priority=True)
        await send_interactive(ctx, pages, block=None)
        if not to_upload:
            return

        view = SimpleCheckView(ctx)
        msg = await ctx.send("Do you want to proceed?", view=view)
        if await view.wait() or view.value is False:
            return

        uploaded = 0

        async def update_message_loop():
            for i in range(5 * 12 * 10):  # timeout progress after 10 minutes
                print(f"Updating msg {uploaded}")
                await msg.edit(content=f"Uploading emojis... ({uploaded}/{len(to_upload)})", view=None)
                await asyncio.sleep(5)

        task = self.bot.loop.create_task(update_message_loop())
        try:
            async with ctx.typing():
                for ball, emote in to_upload:
                    new_emote = await self.bot.create_application_emoji(name=emote.name, image=await emote.read())
                    ball.emoji_id = new_emote.id
                    await ball.asave()
                    uploaded += 1
                    print(f"Uploaded {ball}")
                    await asyncio.sleep(1)
                await self.bot.load_cache()
            task.cancel()
            assert self.bot.application
            await ctx.send(
                f"Successfully migrated {len(to_upload)} emojis. You can check them [here]("
                f"<https://discord.com/developers/applications/{self.bot.application.id}/emojis>)."
            )
        finally:
            task.cancel()

    @commands.command()
    @commands.is_owner()
    async def logs(self, ctx: commands.Context):
        """
        Return the last 50 log entries.
        """
        handler = cast("DequeHandler | None", logging.getHandlerByName("buffer"))
        assert handler is not None

        text = "\n".join(handler.format(record) for record in handler.deque)
        text_display = discord.ui.TextDisplay("")
        view = LayoutView()
        view.add_item(discord.ui.TextDisplay(f"Last {len(handler.deque)} log entries"))
        view.add_item(text_display)
        menu = Menu(self.bot, view, TextSource(text, prefix="```ansi\n", suffix="```"), TextFormatter(text_display))
        await menu.init()
        await ctx.send(view=view)
