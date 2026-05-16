print("START")

import os
import asyncio
import logging
from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands


TOKEN = os.getenv("DISCORD_TOKEN")

# 本番用：30分
CHECK_INTERVAL_SECONDS = 30 * 60

# テストしたい場合は、上をコメントアウトして下を使ってください
# CHECK_INTERVAL_SECONDS = 30

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

sessions = {}


@dataclass
class SleepCheckSession:
    guild_id: int
    text_channel: object
    voice_channel: object
    pressed_user_ids: set = field(default_factory=set)
    active: bool = True
    task: object = None

    def human_members(self):
        return [m for m in self.voice_channel.members if not m.bot]

    def stop(self):
        self.active = False
        if self.task and not self.task.done():
            self.task.cancel()


class DisconnectUserSelect(discord.ui.UserSelect):
    def __init__(self, session):
        super().__init__(
            placeholder="切断するユーザーを選択してください",
            min_values=1,
            max_values=1
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "サーバー内でのみ使用できます。",
                ephemeral=True
            )
            return

        executor = interaction.user

        if not isinstance(executor, discord.Member):
            await interaction.response.send_message(
                "実行者のメンバー情報を取得できませんでした。",
                ephemeral=True
            )
            return

        if not executor.guild_permissions.move_members:
            await interaction.response.send_message(
                "この操作には `メンバーを移動 / Move Members` 権限が必要です。",
                ephemeral=True
            )
            return

        selected_user = self.values[0]

        if not isinstance(selected_user, discord.Member):
            await interaction.response.send_message(
                "選択したユーザーのメンバー情報を取得できませんでした。",
                ephemeral=True
            )
            return

        current_member_ids = {m.id for m in self.session.human_members()}

        if selected_user.id not in current_member_ids:
            await interaction.response.send_message(
                "そのユーザーは対象VCに参加していません。",
                ephemeral=True
            )
            return

        try:
            await selected_user.move_to(
                None,
                reason=f"Disconnected manually by {executor}"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "そのユーザーを切断できませんでした。Botの権限またはロール位置を確認してください。",
                ephemeral=True
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "切断処理中にエラーが発生しました。",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"{selected_user.display_name} をVCから切断しました。",
            ephemeral=True
        )


class DisconnectUserView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=60)
        self.add_item(DisconnectUserSelect(session))


class SleepCheckView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=CHECK_INTERVAL_SECONDS)
        self.session = session

    @discord.ui.button(label="起きてる", style=discord.ButtonStyle.success)
    async def awake_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message(
                "サーバー内でのみ使用できます。",
                ephemeral=True
            )
            return

        if not self.session.active:
            await interaction.response.send_message(
                "この寝落ちチェックはすでに終了しています。",
                ephemeral=True
            )
            return

        member = interaction.user
        current_member_ids = {m.id for m in self.session.human_members()}

        if member.id not in current_member_ids:
            await interaction.response.send_message(
                "対象VCに参加している人だけが押せます。",
                ephemeral=True
            )
            return

        self.session.pressed_user_ids.add(member.id)

        await interaction.response.send_message(
            "確認しました。この30分枠は継続条件を満たしました。",
            ephemeral=True
        )

    @discord.ui.button(label="ユーザーを指定して切断", style=discord.ButtonStyle.danger)
    async def disconnect_user_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message(
                "サーバー内でのみ使用できます。",
                ephemeral=True
            )
            return

        if not self.session.active:
            await interaction.response.send_message(
                "この寝落ちチェックはすでに終了しています。",
                ephemeral=True
            )
            return

        executor = interaction.user

        if not isinstance(executor, discord.Member):
            await interaction.response.send_message(
                "実行者のメンバー情報を取得できませんでした。",
                ephemeral=True
            )
            return

        if not executor.guild_permissions.move_members:
            await interaction.response.send_message(
                "この操作には `メンバーを移動 / Move Members` 権限が必要です。",
                ephemeral=True
            )
            return

        if not self.session.human_members():
            await interaction.response.send_message(
                "対象VCにメンバーがいません。",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "切断するユーザーを選択してください。",
            view=DisconnectUserView(self.session),
            ephemeral=True
        )


async def disconnect_all(session, reason):
    members = session.human_members()
    failed_members = []

    for member in members:
        try:
            await member.move_to(None, reason=reason)
        except discord.Forbidden:
            failed_members.append(member.display_name)
        except discord.HTTPException:
            failed_members.append(member.display_name)

    if failed_members:
        await session.text_channel.send(
            "一部メンバーを切断できませんでした。\n"
            "Botの `Move Members / メンバーを移動` 権限、チャンネル権限、"
            "Botロールの位置を確認してください。\n"
            f"失敗: {', '.join(failed_members)}"
        )


async def sleepcheck_loop(session):
    try:
        while session.active:
            session.pressed_user_ids.clear()

            members = session.human_members()

            if not members:
                await session.text_channel.send(
                    "対象VCが空になったため、寝落ちチェックを終了します。"
                )
                session.stop()
                sessions.pop(session.guild_id, None)
                return

            mentions = " ".join(m.mention for m in members)
            view = SleepCheckView(session)

            await session.text_channel.send(
                f"## 寝落ちチェック\n"
                f"対象VC: {session.voice_channel.mention}\n"
                f"{mentions}\n\n"
                f"{CHECK_INTERVAL_SECONDS // 60}分以内に、誰か1人でも"
                f"「起きてる」ボタンを押してください。\n"
                f"誰も押さなかった場合、対象VC内の全員を切断します。\n\n"
                f"必要な場合は「ユーザーを指定して切断」から個別に切断できます。",
                view=view
            )

            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

            if not session.active:
                return

            current_members = session.human_members()

            if not current_members:
                await session.text_channel.send(
                    "対象VCが空になったため、寝落ちチェックを終了します。"
                )
                session.stop()
                sessions.pop(session.guild_id, None)
                return

            current_member_ids = {m.id for m in current_members}
            valid_pressed_ids = session.pressed_user_ids & current_member_ids

            if not valid_pressed_ids:
                await session.text_channel.send(
                    "30分以内に誰もボタンを押さなかったため、対象VC内の全員を切断します。"
                )

                await disconnect_all(
                    session,
                    reason="Sleep check failed: no one pressed the button"
                )

                session.stop()
                sessions.pop(session.guild_id, None)
                return

            pressed_names = [
                m.display_name
                for m in current_members
                if m.id in valid_pressed_ids
            ]

            await session.text_channel.send(
                f"確認されました。寝落ちチェックを継続します。\n"
                f"押した人: {', '.join(pressed_names)}"
            )

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.exception("sleepcheck_loop error")

        try:
            await session.text_channel.send(
                "寝落ちチェック中にエラーが発生したため停止しました。\n"
                f"`{type(e).__name__}: {e}`"
            )
        except Exception:
            pass

        session.stop()
        sessions.pop(session.guild_id, None)


class SleepCheckGroup(app_commands.Group):
    def __init__(self):
        super().__init__(
            name="sleepcheck",
            description="寝落ち通話用Bot"
        )

    @app_commands.command(
        name="start",
        description="現在入っているVCで寝落ちチェックを開始します"
    )
    async def start(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "サーバー内でのみ使用できます。",
                ephemeral=True
            )
            return

        guild_id = interaction.guild.id

        if guild_id in sessions and sessions[guild_id].active:
            await interaction.response.send_message(
                "このサーバーでは、すでに寝落ちチェックが動いています。",
                ephemeral=True
            )
            return

        member = interaction.user

        if not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                "先に対象のVCへ参加してから実行してください。",
                ephemeral=True
            )
            return

        voice_channel = member.voice.channel
        bot_member = interaction.guild.me

        if bot_member is None:
            await interaction.response.send_message(
                "Bot自身のメンバー情報を取得できませんでした。",
                ephemeral=True
            )
            return

        voice_permissions = voice_channel.permissions_for(bot_member)
        text_permissions = interaction.channel.permissions_for(bot_member)

        if not voice_permissions.move_members:
            await interaction.response.send_message(
                "Botに `Move Members / メンバーを移動` 権限がありません。\n"
                "対象VCのチャンネル権限とBotロールを確認してください。",
                ephemeral=True
            )
            return

        if not text_permissions.send_messages:
            await interaction.response.send_message(
                "Botにこのチャンネルへメッセージを送信する権限がありません。",
                ephemeral=True
            )
            return

        session = SleepCheckSession(
            guild_id=guild_id,
            text_channel=interaction.channel,
            voice_channel=voice_channel
        )

        session.task = asyncio.create_task(sleepcheck_loop(session))
        sessions[guild_id] = session

        await interaction.response.send_message(
            f"{voice_channel.mention} の寝落ちチェックを開始しました。",
            ephemeral=False
        )

    @app_commands.command(
        name="stop",
        description="寝落ちチェックを停止します"
    )
    async def stop(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "サーバー内でのみ使用できます。",
                ephemeral=True
            )
            return

        session = sessions.get(interaction.guild.id)

        if not session or not session.active:
            await interaction.response.send_message(
                "現在動いている寝落ちチェックはありません。",
                ephemeral=True
            )
            return

        session.stop()
        sessions.pop(interaction.guild.id, None)

        await interaction.response.send_message(
            "寝落ちチェックを停止しました。",
            ephemeral=False
        )


synced = False

@bot.event
async def on_ready():
    global synced

    print(f"Logged in as {bot.user}")

    if not synced:
        await bot.tree.sync()
        print("Global slash commands synced")
        synced = True


async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN が設定されていません")

    bot.tree.add_command(SleepCheckGroup())

    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
