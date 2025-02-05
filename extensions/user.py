import logging
import os
import time

import discord
from discord.ext import commands
from discord.utils import escape_markdown, get
from googleapiclient.errors import HttpError
from jinja2 import Template

from utils.gsuite import (
    add_user,
    add_user_group,
    delete_user_group,
    format_google_api_error,
    get_users,
    is_gsuite_admin,
    is_user_managed,
    search_user,
    suspend_user,
    update_user_department,
    update_user_password,
    update_user_pseudo,
    update_user_recovery,
    update_user_signature,
)
from utils.LouisDeLaTechError import LouisDeLaTechError
from utils.password import generate_password
from utils.User import User

logger = logging.getLogger(__name__)


class UserCog(commands.Cog):
    @commands.command(help="Provision an user")
    @commands.guild_only()
    @is_gsuite_admin
    async def provision(
        self, ctx, member: discord.Member, firstname, lastname, pseudo, role_name
    ):
        """
        Provision an user
        [Discord]
            => User will be added to default group
            => User will be added to team group
        [Google]
            => User will be created and added to team group
        """
        user_email = User.email_from_name(firstname, lastname)
        user_team = self.bot.config["teams"].get(role_name, None)
        password = generate_password()
        admin_sdk = self.bot.admin_sdk()
        signature_template = Template(
            open(
                os.path.join(
                    self.bot.root_dir, "./templates/google/gmail_signature.j2"
                ),
                encoding="utf-8",
            ).read()
        )

        if user_team is None:
            await ctx.send(f"Role {role_name} is not managed by bot")
            return
        elif not user_team["team_role"]:
            await ctx.send(f"Role {role_name} is not a team role")
            return

        try:
            add_user(
                admin_sdk,
                firstname,
                lastname,
                user_email,
                password,
                role_name,
                member.id,
                pseudo,
            )
            add_user_group(admin_sdk, user_email, user_team["google_email"])

            # force time sleep or refresh token will cause an error
            # maybe API caching issue (if request is too fast)
            time.sleep(5)

            update_user_signature(
                self.bot.gmail_sdk(user_email),
                signature_template,
                user_email,
                firstname,
                lastname,
                None,
                role_name,
                user_team["team_role"],
            )
        except HttpError as e:
            await ctx.send(format_google_api_error(e))
            raise

        for role_name in self.bot.config["discord"]["roles"]["default"]:
            role = get(member.guild.roles, name=role_name)
            if role:
                await member.add_roles(role)
            else:
                await ctx.send(
                    f"Discord role {role_name} does not exist on server, check bot config"
                )
                return
        role = get(member.guild.roles, name=user_team["discord"])
        if role:
            await member.add_roles(role)
        else:
            await ctx.send(f"Discord role {role_name} does not exist on discord server")
            return

        await member.edit(nick=User.discord_name(firstname, pseudo, lastname))

        await ctx.send(f"User {user_email} provisionned")

        template = Template(
            open(
                os.path.join(self.bot.root_dir, "./templates/discord/base.j2"),
                encoding="utf-8",
            ).read()
        )
        await member.send(
            template.render(
                {"email": user_email, "password": escape_markdown(password)}
            )
        )

        template = Template(
            open(
                os.path.join(
                    self.bot.root_dir,
                    f"./templates/discord/{user_team['message_template']}",
                ),
                encoding="utf-8",
            ).read()
        )
        team_message = template.render()
        if team_message:
            await member.send(team_message)

    def __init__(self, bot):
        self.bot = bot

    @commands.command(help="Deprovision an user")
    @commands.guild_only()
    @is_gsuite_admin
    async def deprovision(self, ctx, member: discord.Member):
        """
        [Discord]
            => User will be removed from all groups
        [Google]
            => User will be suspended
        """
        try:
            user = User(search_user(self.bot.admin_sdk(), member.name, member.id))
            is_user_managed(
                self.bot.admin_sdk(), user.email, self.bot.config["teams_to_skip"]
            )
        except LouisDeLaTechError as e:
            await ctx.send(f"{member} => {e.args[0]}")
            return
        except HttpError as e:
            await ctx.send(format_google_api_error(e))
            raise

        try:
            suspend_user(self.bot.admin_sdk(), user.email)
        except HttpError as e:
            await ctx.send(format_google_api_error(e))
            raise

        await member.edit(roles=[])

        await ctx.send(f"User {member.name} deprovisionned")

    @commands.command(name="uteam", help="Update user team")
    @commands.guild_only()
    @is_gsuite_admin
    async def update_team(self, ctx, member: discord.Member, new_team_name):
        """
        [Discord]
            => User will be removed from all team groups
            => User will be added to this new team
        [Google]
            => User will be removed from all team groups
            => User will be added to this new team
            => User signature will be updated
        """
        try:
            user = User(search_user(self.bot.admin_sdk(), member.name, member.id))
            is_user_managed(
                self.bot.admin_sdk(), user.email, self.bot.config["teams_to_skip"]
            )
            user.team = new_team_name
        except LouisDeLaTechError as e:
            await ctx.send(f"{member} => {e.args[0]}")
            return
        except HttpError as e:
            await ctx.send(format_google_api_error(e))
            raise

        new_user_team = self.bot.config["teams"].get(user.team, None)
        admin_sdk = self.bot.admin_sdk()
        signature_template = Template(
            open(
                os.path.join(
                    self.bot.root_dir, "./templates/google/gmail_signature.j2"
                ),
                encoding="utf-8",
            ).read()
        )

        if new_user_team is None:
            await ctx.send(f"Role {new_team_name} does not exist, check bot config")
            return
        elif not new_user_team["team_role"]:
            await ctx.send(f"Role {new_user_team} is invalid, check bot config")
            return

        try:
            for v in self.bot.config["teams"].values():
                delete_user_group(admin_sdk, user.email, v["google_email"])
            add_user_group(admin_sdk, user.email, new_user_team["google_email"])
            update_user_department(admin_sdk, user.email, user.team)
            update_user_signature(
                self.bot.gmail_sdk(user.email),
                signature_template,
                user.email,
                user.firstname,
                user.lastname,
                user.role,
                user.team,
                new_user_team["team_role"],
            )
        except HttpError as e:
            await ctx.send(format_google_api_error(e))
            raise

        if new_user_team is None:
            await ctx.send(f"Role {user.team} does not exist, check bot config")
            return

        for v in self.bot.config["teams"].values():
            role = get(member.guild.roles, name=v["discord"])
            if role:
                await member.remove_roles(role)
            else:
                await ctx.send(
                    f"Discord role {v['discord']} does not exist, check bot config"
                )
                return
        role = get(member.guild.roles, name=new_user_team["discord"])
        if role:
            await member.add_roles(role)
        else:
            await ctx.send(f"Discord role {user.team} does not exist")
            return

        await ctx.send(f"User {member.name} is now member of team: {user.team}")

    @commands.command(name="upseudo", help="Update user pseudo")
    @commands.guild_only()
    @is_gsuite_admin
    async def update_pseudo(self, ctx, member: discord.Member, new_pseudo):
        """
        [Discord]
            => User will be renamed
        [Google]
            => User pseudo will be renamed
        """
        try:
            user = User(search_user(self.bot.admin_sdk(), member.name, member.id))
            is_user_managed(
                self.bot.admin_sdk(), user.email, self.bot.config["teams_to_skip"]
            )
            user.pseudo = new_pseudo
        except LouisDeLaTechError as e:
            await ctx.send(f"{member} => {e.args[0]}")
            return
        except HttpError as e:
            await ctx.send(format_google_api_error(e))
            raise

        try:
            update_user_pseudo(
                self.bot.admin_sdk(),
                user.email,
                user.pseudo,
            )
        except HttpError as e:
            await ctx.send(format_google_api_error(e))
            raise

        old_nick = member.nick

        await member.edit(
            nick=User.discord_name(user.firstname, user.pseudo, user.lastname)
        )

        await ctx.send(f"User {old_nick} you now shall be called {member.nick} !")

    @commands.command(
        name="usignatures", help="Update the signature of all users on gmail"
    )
    @commands.guild_only()
    @is_gsuite_admin
    async def update_signatures(self, ctx):
        user_updated = 0
        try:
            users = get_users(self.bot.admin_sdk())
        except HttpError as e:
            await ctx.send(format_google_api_error(e))
            raise

        signature_template = Template(
            open(
                os.path.join(
                    self.bot.root_dir, "./templates/google/gmail_signature.j2"
                ),
                encoding="utf-8",
            ).read()
        )

        await ctx.send(f"Starting to update {len(users)} users")
        for _user in users:
            try:
                user = User(_user)
                is_user_managed(
                    self.bot.admin_sdk(), user.email, self.bot.config["teams_to_skip"]
                )
                user_team = self.bot.config["teams"].get(user.team, None)
                update_user_signature(
                    self.bot.gmail_sdk(user.email),
                    signature_template,
                    user.email,
                    user.firstname,
                    user.lastname,
                    user.role,
                    user.team,
                    user_team["team_role"],
                )
                user_updated += 1
            except LouisDeLaTechError as e:
                await ctx.send(f"{e.args[0]}")
                continue
            except HttpError as e:
                await ctx.send(format_google_api_error(e))
                return

        await ctx.send(f"Updated signatures for {user_updated}/{len(users)} users")

    @commands.command(help="Update recovery email of an user")
    @commands.guild_only()
    @is_gsuite_admin
    async def urecovery(self, ctx, member: discord.Member, backup_email):
        try:
            user = User(search_user(self.bot.admin_sdk(), member.name, member.id))
            is_user_managed(
                self.bot.admin_sdk(), user.email, self.bot.config["teams_to_skip"]
            )
        except LouisDeLaTechError as e:
            await ctx.send(f"{member} => {e.args[0]}")
            return
        except HttpError as e:
            await ctx.send(format_google_api_error(e))
            raise
        try:
            update_user_recovery(self.bot.admin_sdk(), user.email, backup_email)
        except HttpError as e:
            await ctx.send(format_google_api_error(e))
            raise

        await ctx.send(f"Updated recovery information for user {member.name}")

    @commands.command(help="Reset password of an user")
    @commands.guild_only()
    @is_gsuite_admin
    async def rpassword(self, ctx, member: discord.Member):
        try:
            user = User(search_user(self.bot.admin_sdk(), member.name, member.id))
            is_user_managed(
                self.bot.admin_sdk(), user.email, self.bot.config["teams_to_skip"]
            )
        except LouisDeLaTechError as e:
            await ctx.send(f"{member} => {e.args[0]}")
            return
        except HttpError as e:
            await ctx.send(format_google_api_error(e))
            raise

        temp_pass = generate_password()

        template = Template(
            open(
                os.path.join(
                    self.bot.root_dir, "./templates/discord/reset_password.j2"
                ),
                encoding="utf-8",
            ).read()
        )

        try:
            update_user_password(self.bot.admin_sdk(), user.email, temp_pass, True)
        except HttpError as e:
            await ctx.send(format_google_api_error(e))
            raise

        await member.send(
            template.render(
                {"email": user.email, "password": escape_markdown(temp_pass)}
            )
        )
        await ctx.send(f"Sent a new password to {member.name} in PM")


def setup(bot):
    bot.add_cog(UserCog(bot))
