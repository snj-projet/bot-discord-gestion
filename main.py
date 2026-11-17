import asyncio
import copy
import json
import os
import random
import re
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands
from discord.ui import View, Button, Modal, TextInput


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "snj_config.json"
DATA_PATH = BASE_DIR / "snj_data.json"

DEFAULT_OWNERS = [1258616348377092122]
COLOR = discord.Color.dark_purple()
LINK_RE = re.compile(r"(https?://|www\.|discord\.gg/)\S*", re.IGNORECASE)
DURATION_RE = re.compile(r"^(\d+)\s*(s|m|h|d|j)?$", re.IGNORECASE)
MAX_TIMEOUT = timedelta(days=28)

DEFAULT_GUILD = {
    "antilink": False,
    "antieveryone": True,
    "antiraid": False,
    "raid_joins": 10,
    "raid_seconds": 60,
    "raid_action": "alert",
    "antispam": False,
    "spam_messages": 6,
    "spam_seconds": 5,
    "spam_timeout_minutes": 10,
    "badwords": [],
    "welcome_enabled": False,
    "welcome_channel": None,
    "welcome_message": "Bienvenue {member} sur **{server}** ! Nous sommes maintenant {count} membres.",
    "leave_enabled": False,
    "leave_channel": None,
    "leave_message": "**{name}** a quitté le serveur. Nous sommes maintenant {count} membres.",
    "autorole": None,
    "log_messages": None,
    "log_voice": None,
    "log_mod": None,
    "warn_threshold": 3,
    "warn_action": "timeout",
    "warn_timeout_minutes": 60,
    "whitelist": [],
    "blacklist": [],
    "warns": {},
}


class Store:
    """Données persistantes du bot (réglages par serveur, owners, giveaways)."""

    def __init__(self, path):
        self.path = path
        self.data = {
            "owners": list(DEFAULT_OWNERS),
            "prefix": "+",
            "status": "online",
            "activity": "",
            "guilds": {},
            "giveaways": [],
        }
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))

    def guild(self, guild_id):
        cfg = self.data["guilds"].setdefault(str(guild_id), {})
        for key, value in DEFAULT_GUILD.items():
            if key not in cfg:
                cfg[key] = copy.deepcopy(value)
        return cfg

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)


def load_config():
    config = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)

    if "token" not in config:
        config["token"] = ""
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    config["token"] = os.environ.get("DISCORD_TOKEN") or config["token"]
    return config


config = load_config()
store = Store(DATA_PATH)

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.members = True
intents.message_content = True
intents.voice_states = True


class SnjBot(commands.Bot):
    async def setup_hook(self):
        self.start_time = time.time()
        for gw in list(store.data["giveaways"]):
            spawn(run_giveaway(gw))


bot = SnjBot(command_prefix=lambda b, m: store.data["prefix"], intents=intents, help_command=None)

background_tasks = set()
join_times = defaultdict(deque)
message_times = defaultdict(deque)
snipes = {}


def spawn(coro):
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


def is_owner():
    def predicate(ctx):
        return ctx.author.id in store.data["owners"]
    return commands.check(predicate)


def is_immune(member, cfg):
    return (
        member.id in store.data["owners"]
        or member.id in cfg["whitelist"]
        or member.id == member.guild.owner_id
    )


def cut(text, limit=1024):
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def parse_duration(text):
    """'30s', '10m', '2h', '1j' -> timedelta. Un nombre seul est en minutes."""
    match = DURATION_RE.match(text or "")
    if not match:
        return None
    amount = int(match.group(1))
    unit = (match.group(2) or "m").lower()
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400, "j": 86400}[unit]
    return timedelta(seconds=amount * seconds) if amount > 0 else None


def format_duration(delta):
    seconds = int(delta.total_seconds())
    for size, label in ((86400, "j"), (3600, "h"), (60, "min")):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size} {label}"
    return f"{seconds} s"


def fill_template(template, member):
    return (
        template.replace("{member}", member.mention)
        .replace("{name}", member.display_name)
        .replace("{server}", member.guild.name)
        .replace("{count}", str(member.guild.member_count))
    )


def action_error(ctx, member):
    """Renvoie un message d'erreur si le bot ne doit/peut pas sanctionner ce membre."""
    cfg = store.guild(ctx.guild.id)
    if member.id == ctx.author.id:
        return "Vous ne pouvez pas vous sanctionner vous-même."
    if member.id in store.data["owners"]:
        return f"{member.mention} est un propriétaire du bot."
    if member.id in cfg["whitelist"]:
        return f"{member.mention} est dans la whitelist."
    if member.id == ctx.guild.owner_id or member.top_role >= ctx.guild.me.top_role:
        return f"Mon rôle n'est pas assez haut pour agir sur {member.mention}."
    return None


async def safe_delete(message):
    try:
        await message.delete()
    except discord.HTTPException:
        pass


async def send_log(guild, kind, embed):
    channel_id = store.guild(guild.id).get(f"log_{kind}")
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


def log_embed(title, description=None, color=None):
    return discord.Embed(
        title=title, description=description,
        color=color or discord.Color.blurple(), timestamp=discord.utils.utcnow(),
    )


async def audit_entry(guild, action, target_id=None, within=15):
    try:
        async for entry in guild.audit_logs(limit=6, action=action):
            if (discord.utils.utcnow() - entry.created_at).total_seconds() > within:
                continue
            if target_id is not None and getattr(entry.target, "id", None) != target_id:
                continue
            return entry
    except discord.HTTPException:
        pass
    return None


async def apply_presence():
    status = getattr(discord.Status, store.data["status"], discord.Status.online)
    activity = discord.Game(name=store.data["activity"]) if store.data["activity"] else None
    await bot.change_presence(status=status, activity=activity)

@bot.event
async def on_ready():
    await apply_presence()
    print(f'Bot connecté en tant que {bot.user}')


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ Vous n'avez pas la permission d'utiliser cette commande.", delete_after=5)
    elif isinstance(error, commands.MissingRequiredArgument):
        prefix = store.data["prefix"]
        await ctx.send(f"Utilisation : `{prefix}{ctx.command.qualified_name} {ctx.command.signature}`", delete_after=10)
    elif isinstance(error, commands.BadArgument):
        await ctx.send("🚫 Argument invalide (membre, salon ou nombre introuvable).", delete_after=5)
    elif isinstance(error, commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
        await ctx.send("🚫 Je n'ai pas les permissions nécessaires pour faire ça.", delete_after=5)
    else:
        await ctx.send(f"🚫 Erreur : {cut(str(getattr(error, 'original', error)), 300)}", delete_after=10)
        print(f"[erreur] {ctx.command}: {error!r}")


async def automod(message, cfg):
    """Applique les filtres. Renvoie True si le message a été supprimé."""
    content = message.content
    reason = None

    if cfg["antieveryone"] and ("@everyone" in content or "@here" in content):
        reason = "vous n'êtes pas autorisé à utiliser everyone ou here !"
    elif cfg["antilink"] and LINK_RE.search(content):
        reason = "les liens ne sont pas autorisés !"
    elif cfg["badwords"]:
        lowered = content.lower()
        if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in cfg["badwords"]):
            reason = "ce mot est interdit ici !"

    if reason:
        await safe_delete(message)
        await message.channel.send(f"{message.author.mention}, {reason}", delete_after=5)
        return True

    if cfg["antispam"]:
        now = time.time()
        times = message_times[(message.guild.id, message.author.id)]
        times.append(now)
        while times and now - times[0] > cfg["spam_seconds"]:
            times.popleft()
        if len(times) >= cfg["spam_messages"]:
            times.clear()
            minutes = cfg["spam_timeout_minutes"]
            try:
                await message.author.timeout(timedelta(minutes=minutes), reason="Anti-spam")
            except discord.HTTPException:
                return False
            await message.channel.send(f"{message.author.mention} a été mute {minutes} min pour spam.", delete_after=10)
            await send_log(message.guild, "mod", log_embed(
                "🚨 Anti-spam", f"{message.author.mention} a été mute {minutes} min pour spam dans {message.channel.mention}.",
                discord.Color.red()))
    return False


@bot.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return

    cfg = store.guild(message.guild.id)
    if not is_immune(message.author, cfg) and await automod(message, cfg):
        return

    if message.attachments or LINK_RE.search(message.content):
        embed = log_embed("📨 Message contenant un lien ou un fichier", color=discord.Color.blue())
        embed.add_field(name="Auteur", value=message.author.mention, inline=True)
        embed.add_field(name="Salon", value=message.channel.mention, inline=True)
        embed.add_field(name="Contenu", value=cut(message.content) or "*Vide*", inline=False)
        if message.attachments:
            embed.add_field(name="Fichiers", value=cut("\n".join(a.url for a in message.attachments)), inline=False)
        await send_log(message.guild, "messages", embed)

    await bot.process_commands(message)


@bot.event
async def on_message_delete(message):
    if message.guild is None or message.author.bot:
        return

    snipes[message.channel.id] = (str(message.author), message.content, discord.utils.utcnow())

    embed = log_embed("🗑️ Message supprimé", color=discord.Color.red())
    embed.add_field(name="Auteur", value=message.author.mention, inline=True)
    embed.add_field(name="Salon", value=message.channel.mention, inline=True)
    embed.add_field(name="Contenu", value=cut(message.content) or "*Aucun contenu*", inline=False)
    entry = await audit_entry(message.guild, discord.AuditLogAction.message_delete, message.author.id, within=5)
    if entry and entry.user != message.author:
        embed.set_footer(text=f"Supprimé par {entry.user.name}")
    await send_log(message.guild, "messages", embed)


@bot.event
async def on_message_edit(before, after):
    if before.guild is None or before.author.bot or before.content == after.content:
        return

    embed = log_embed("✏️ Message modifié", color=discord.Color.orange())
    embed.add_field(name="Auteur", value=before.author.mention, inline=True)
    embed.add_field(name="Salon", value=before.channel.mention, inline=True)
    embed.add_field(name="Avant", value=cut(before.content) or "*Vide*", inline=False)
    embed.add_field(name="Après", value=cut(after.content) or "*Vide*", inline=False)
    await send_log(before.guild, "messages", embed)

    cfg = store.guild(after.guild.id)
    if isinstance(after.author, discord.Member) and not is_immune(after.author, cfg):
        await automod(after, dict(cfg, antispam=False))


@bot.event
async def on_member_join(member):
    guild = member.guild
    cfg = store.guild(guild.id)

    if member.id in cfg["blacklist"]:
        try:
            await member.ban(reason="Membre blacklisté")
        except discord.HTTPException:
            pass
        return

    if cfg["antiraid"]:
        now = time.time()
        joins = join_times[guild.id]
        joins.append(now)
        while joins and now - joins[0] > cfg["raid_seconds"]:
            joins.popleft()
        if len(joins) > cfg["raid_joins"]:
            alert = log_embed(
                "🚨 Alerte de raid",
                f"{len(joins)} arrivées en moins de {cfg['raid_seconds']} s (dernier : {member.mention}).",
                discord.Color.red())
            await send_log(guild, "mod", alert)
            if cfg["raid_action"] == "kick":
                try:
                    await member.kick(reason="Anti-raid")
                except discord.HTTPException:
                    pass
                return
            if guild.system_channel and len(joins) == cfg["raid_joins"] + 1:
                try:
                    await guild.system_channel.send(embed=alert)
                except discord.HTTPException:
                    pass

    if cfg["autorole"]:
        role = guild.get_role(cfg["autorole"])
        if role:
            try:
                await member.add_roles(role, reason="Rôle automatique")
            except discord.HTTPException:
                pass

    if cfg["welcome_enabled"] and cfg["welcome_channel"]:
        channel = guild.get_channel(cfg["welcome_channel"])
        if channel:
            embed = discord.Embed(description=fill_template(cfg["welcome_message"], member), color=COLOR)
            embed.set_thumbnail(url=member.display_avatar.url)
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass


@bot.event
async def on_member_remove(member):
    guild = member.guild
    cfg = store.guild(guild.id)

    if cfg["leave_enabled"] and cfg["leave_channel"]:
        channel = guild.get_channel(cfg["leave_channel"])
        if channel:
            try:
                await channel.send(embed=discord.Embed(description=fill_template(cfg["leave_message"], member), color=COLOR))
            except discord.HTTPException:
                pass

    entry = await audit_entry(guild, discord.AuditLogAction.kick, member.id, within=10)
    if entry:
        embed = log_embed("🥾 Membre kické", color=discord.Color.orange())
        embed.add_field(name="Utilisateur", value=f"{member.mention} ({member})")
        if entry.reason:
            embed.add_field(name="Raison", value=cut(entry.reason), inline=False)
        embed.set_footer(text=f"Kické par {entry.user.name}")
        await send_log(guild, "mod", embed)


@bot.event
async def on_member_ban(guild, user):
    entry = await audit_entry(guild, discord.AuditLogAction.ban, user.id)
    embed = log_embed("🔨 Membre banni", color=discord.Color.dark_red())
    embed.add_field(name="Utilisateur", value=f"{user.mention} ({user})")
    if entry:
        if entry.reason:
            embed.add_field(name="Raison", value=cut(entry.reason), inline=False)
        embed.set_footer(text=f"Banni par {entry.user.name}")
    await send_log(guild, "mod", embed)


@bot.event
async def on_member_unban(guild, user):
    cfg = store.guild(guild.id)
    if user.id in cfg["blacklist"]:
        try:
            await guild.ban(user, reason="Membre blacklisté : débannissement annulé")
        except discord.HTTPException:
            pass
        await send_log(guild, "mod", log_embed(
            "⛔ Débannissement annulé", f"{user.mention} est blacklisté et a été rebanni.", discord.Color.dark_red()))
        return

    entry = await audit_entry(guild, discord.AuditLogAction.unban, user.id)
    embed = log_embed("🔓 Membre débanni", color=discord.Color.green())
    embed.add_field(name="Utilisateur", value=f"{user.mention} ({user})")
    if entry:
        embed.set_footer(text=f"Débanni par {entry.user.name}")
    await send_log(guild, "mod", embed)


@bot.event
async def on_member_update(before, after):
    if before.timed_out_until == after.timed_out_until:
        return

    if after.is_timed_out():
        title = "⏳ Timeout appliqué"
        desc = f"{after.mention} est timeout jusqu'à {discord.utils.format_dt(after.timed_out_until, style='R')}"
    else:
        title = "✅ Timeout retiré"
        desc = f"{after.mention} n’est plus timeout."

    embed = log_embed(title, desc, discord.Color.blue())
    entry = await audit_entry(after.guild, discord.AuditLogAction.member_update, after.id)
    if entry:
        if entry.reason:
            embed.add_field(name="Raison", value=cut(entry.reason), inline=False)
        embed.set_footer(text=f"Par {entry.user.name}")
    await send_log(after.guild, "mod", embed)


async def voice_flag_log(member, attr, new_value, title, on_text, off_text):
    await asyncio.sleep(1)
    entry = await audit_entry(member.guild, discord.AuditLogAction.member_update, member.id, within=30)
    if entry and getattr(entry.after, attr, None) != new_value:
        entry = None
    action = on_text if new_value else off_text
    desc = f"{member.mention} a été **{action}** côté serveur"
    embed = log_embed(title, desc + (f" par {entry.user.mention}." if entry else "."))
    if entry and entry.reason:
        embed.add_field(name="Raison", value=cut(entry.reason), inline=False)
    await send_log(member.guild, "voice", embed)


@bot.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    if member.bot or not store.guild(guild.id)["log_voice"]:
        return

    if before.channel != after.channel:
        if before.channel is None:
            embed = log_embed("🎙️ Connexion vocale", f"{member.mention} s'est connecté à {after.channel.mention}")
        elif after.channel is None:
            await asyncio.sleep(1)
            entry = await audit_entry(guild, discord.AuditLogAction.member_disconnect, within=30)
            if entry:
                desc = f"{member.mention} a été déconnecté de {before.channel.mention} par {entry.user.mention}."
            else:
                desc = f"{member.mention} s'est déconnecté de {before.channel.mention}"
            embed = log_embed("❌ Déconnexion vocale", desc)
        else:
            await asyncio.sleep(1)
            entry = await audit_entry(guild, discord.AuditLogAction.member_move, within=30)
            moved_to = getattr(getattr(entry, "extra", None), "channel", None)
            if entry and getattr(moved_to, "id", after.channel.id) == after.channel.id:
                embed = log_embed(
                    "🚚 Déplacement vocal",
                    f"{member.mention} a été déplacé de {before.channel.mention} à {after.channel.mention} par {entry.user.mention}.")
            else:
                embed = log_embed(
                    "🔄 Changement de salon vocal",
                    f"{member.mention} est passé de {before.channel.mention} à {after.channel.mention}")
        await send_log(guild, "voice", embed)

    if before.self_stream != after.self_stream:
        state = 'activé' if after.self_stream else 'désactivé'
        await send_log(guild, "voice", log_embed("📺 Partage d'écran", f"{member.mention} a {state} son partage d'écran."))

    if before.mute != after.mute:
        await voice_flag_log(member, "mute", after.mute, "🔇 Mute micro (serveur)", "muté", "démuté")

    if before.deaf != after.deaf:
        await voice_flag_log(member, "deaf", after.deaf, "🔈 Mute casque (serveur)", "rendu sourd", "rendu entendant")

    if before.self_mute != after.self_mute:
        action = "s'est muté" if after.self_mute else "s'est démuté"
        await send_log(guild, "voice", log_embed("🤫 Mute micro (personnel)", f"{member.mention} **{action}**."))

    if before.self_deaf != after.self_deaf:
        action = "s'est rendu sourd" if after.self_deaf else "a remis le son"
        await send_log(guild, "voice", log_embed("🙉 Mute casque (personnel)", f"{member.mention} **{action}**."))



HELP_PAGES = [
    ("🛡️ Protection", [
        ("antiraid_on / antiraid_off", "Active ou désactive l'anti-raid."),
        ("antilink_on / antilink_off", "Active ou désactive le filtre de liens."),
        ("antispam_on / antispam_off", "Active ou désactive l'anti-spam (mute automatique)."),
        ("antieveryone_on / antieveryone_off", "Bloque ou autorise @everyone / @here."),
        ("badword add|remove|list <mot>", "Gère la liste des mots interdits."),
        ("wl / unwl <@membre>", "Ajoute ou retire un membre de la whitelist (ignoré par les filtres)."),
        ("bl / unbl <@membre> [raison]", "Blacklist (ban permanent, rebanni s'il revient). `bl` seul affiche la liste."),
        ("own / unown <@membre>", "Ajoute ou retire un propriétaire du bot."),
    ]),
    ("🔨 Modération", [
        ("ban <@membre> [raison]", "Bannit un membre."),
        ("unban <id> [raison]", "Débannit un utilisateur."),
        ("kick <@membre> [raison]", "Expulse un membre."),
        ("mute <@membre> [durée] [raison]", "Mute un membre (ex : 10m, 2h, 1j — 28 j par défaut)."),
        ("unmute <@membre>", "Démute un membre."),
        ("warn <@membre> [raison]", "Avertit un membre (sanction automatique au seuil configuré)."),
        ("warns <@membre>", "Affiche les avertissements d'un membre."),
        ("unwarn <@membre> [numéro]", "Retire un avertissement (le dernier par défaut)."),
        ("clearwarns <@membre>", "Efface tous les avertissements d'un membre."),
        ("clear <nombre>", "Supprime des messages du salon."),
    ]),
    ("⚙️ Gestion", [
        ("lock / unlock [#salon]", "Verrouille ou déverrouille un salon."),
        ("lockall / unlockall", "Verrouille ou déverrouille tous les salons textuels."),
        ("slowmode <secondes>", "Règle le mode lent du salon (0 pour désactiver)."),
        ("renew", "Refait le salon à l'identique."),
        ("delete_channel <id>", "Supprime un salon."),
        ("rank / derank <@membre>", "Donne ou retire tous les rôles d'un membre."),
        ("nick <@membre> <pseudo>", "Change le pseudo d'un membre."),
        ("addemoji <emoji ou id> [nom]", "Ajoute un emoji au serveur."),
        ("logs", "Crée les salons de logs."),
    ]),
    ("🎉 Communauté", [
        ("welcome <#salon> [message] / welcome off", "Message de bienvenue ({member}, {name}, {server}, {count})."),
        ("leave <#salon> [message] / leave off", "Message de départ."),
        ("autorole <@rôle> / autorole off", "Rôle donné automatiquement aux nouveaux."),
        ("giveaway <durée> <gain>", "Lance un giveaway (ex : 30m, 2h, 1j)."),
        ("reroll <id du message>", "Retire un gagnant pour un giveaway terminé."),
        ("poll <question> | <choix 1> | <choix 2>", "Crée un sondage (sans choix : oui / non)."),
        ("embed", "Crée un embed avec un formulaire."),
        ("say <message>", "Fait parler le bot."),
    ]),
    ("ℹ️ Infos & bot", [
        ("help", "Affiche cette aide."),
        ("serverinfo", "Infos du serveur."),
        ("userinfo [@membre]", "Infos sur un membre."),
        ("avatar [@membre]", "Affiche l'avatar d'un membre."),
        ("snipe", "Affiche le dernier message supprimé du salon."),
        ("ping", "Latence et uptime du bot."),
        ("config", "Résumé des réglages du serveur."),
        ("prefix <préfixe>", "Change le préfixe du bot."),
        ("setstatus <online/idle/dnd/invisible>", "Change le statut du bot."),
        ("setactivity <activité>", "Change l'activité du bot."),
    ]),
]


class HelpView(View):
    def __init__(self, author_id):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.page = 0

    def embed(self):
        prefix = store.data["prefix"]
        title, entries = HELP_PAGES[self.page]
        embed = discord.Embed(title=f"Liste des Commandes — {title}", color=discord.Color.pink())
        for usage, description in entries:
            embed.add_field(name=f"{prefix}{usage}", value=description, inline=False)
        embed.set_footer(text=f"Page {self.page + 1}/{len(HELP_PAGES)}")
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Ce menu n'est pas le vôtre.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: Button):
        self.page = (self.page - 1) % len(HELP_PAGES)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: Button):
        self.page = (self.page + 1) % len(HELP_PAGES)
        await interaction.response.edit_message(embed=self.embed(), view=self)


@bot.command(name='help')
async def help_command(ctx):
    view = HelpView(ctx.author.id)
    await ctx.send(embed=view.embed(), view=view)


@bot.command(name='own')
@is_owner()
async def owner(ctx, member: discord.Member):
    if member.id not in store.data["owners"]:
        store.data["owners"].append(member.id)
        store.save()
        await ctx.send(f'{member.mention} a été ajouté en tant que propriétaire du bot !')
    else:
        await ctx.send(f'{member.mention} est déjà un propriétaire du bot.')


@bot.command(name='unown')
@is_owner()
async def remove_owner(ctx, member: discord.Member):
    owners = store.data["owners"]
    if member.id not in owners:
        await ctx.send(f"{member.mention} n'est pas un propriétaire du bot.")
    elif len(owners) == 1:
        await ctx.send("Impossible de retirer le dernier propriétaire du bot.")
    else:
        owners.remove(member.id)
        store.save()
        await ctx.send(f'{member.mention} a été retiré de la liste des propriétaires du bot.')


@bot.command(name='wl')
@is_owner()
async def whitelist(ctx, member: discord.Member = None):
    cfg = store.guild(ctx.guild.id)
    if member is None:
        names = '\n'.join(f"<@{uid}>" for uid in cfg["whitelist"]) or "Aucun membre dans la whitelist."
        await ctx.send(embed=discord.Embed(title="Whitelist", description=names, color=COLOR))
    elif member.id not in cfg["whitelist"]:
        cfg["whitelist"].append(member.id)
        store.save()
        await ctx.send(f'{member.mention} a été ajouté à la whitelist !')
    else:
        await ctx.send(f'{member.mention} est déjà dans la whitelist.')


@bot.command(name='unwl')
@is_owner()
async def unwhitelist(ctx, member: discord.Member):
    cfg = store.guild(ctx.guild.id)
    if member.id in cfg["whitelist"]:
        cfg["whitelist"].remove(member.id)
        store.save()
        await ctx.send(f'{member.mention} a été retiré de la whitelist !')
    else:
        await ctx.send(f'{member.mention} n\'est pas dans la whitelist.')


@bot.command(name='bl')
@is_owner()
async def blacklist(ctx, user: discord.User = None, *, reason=None):
    cfg = store.guild(ctx.guild.id)
    if user is None:
        names = '\n'.join(f"<@{uid}> (`{uid}`)" for uid in cfg["blacklist"]) or "Aucun membre dans la blacklist."
        await ctx.send(embed=discord.Embed(title="Blacklist", description=names, color=COLOR))
    elif user.id in store.data["owners"]:
        await ctx.send(f'{user.mention} est un propriétaire et ne peut pas être blacklisté.')
    elif user.id in cfg["blacklist"]:
        await ctx.send(f'{user.mention} est déjà dans la blacklist.')
    else:
        cfg["blacklist"].append(user.id)
        store.save()
        await ctx.guild.ban(user, reason=reason or "Ajouté à la blacklist")
        await ctx.send(f'{user.mention} a été ajouté à la blacklist et banni pour : {reason}')


@bot.command(name='unbl')
@is_owner()
async def unblacklist(ctx, user: discord.User):
    cfg = store.guild(ctx.guild.id)
    if user.id in cfg["blacklist"]:
        cfg["blacklist"].remove(user.id)
        store.save()
        try:
            await ctx.guild.unban(user, reason="Retiré de la blacklist")
        except discord.HTTPException:
            pass
        await ctx.send(f'{user.mention} a été retiré de la blacklist !')
    else:
        await ctx.send(f'{user.mention} n\'est pas dans la blacklist.')


def make_toggle(key, state, text):
    async def toggle(ctx):
        store.guild(ctx.guild.id)[key] = state
        store.save()
        await ctx.send(text)
    return toggle


for _key, _label in (
    ("antiraid", "L'anti-raid"),
    ("antilink", "Le filtre de liens"),
    ("antispam", "L'anti-spam"),
    ("antieveryone", "Le filtre everyone/here"),
):
    for _state, _word in ((True, "activé"), (False, "désactivé")):
        _callback = is_owner()(make_toggle(_key, _state, f"{_label} est maintenant {_word}."))
        bot.command(name=f"{_key}_{'on' if _state else 'off'}")(_callback)


@bot.command(name='badword')
@is_owner()
async def badword(ctx, action: str, *, word: str = None):
    words = store.guild(ctx.guild.id)["badwords"]
    action = action.lower()
    word = word.lower().strip() if word else None

    if action == "list":
        await ctx.send(embed=discord.Embed(
            title="Mots interdits", description=cut(", ".join(words), 4000) or "Aucun mot interdit.", color=COLOR))
    elif action == "add" and word:
        if word not in words:
            words.append(word)
            store.save()
        await ctx.send("✅ Mot ajouté à la liste des mots interdits.", delete_after=5)
        await safe_delete(ctx.message)
    elif action == "remove" and word:
        if word in words:
            words.remove(word)
            store.save()
            await ctx.send("✅ Mot retiré de la liste.", delete_after=5)
        else:
            await ctx.send("Ce mot n'est pas dans la liste.", delete_after=5)
    else:
        await ctx.send(f"Utilisation : `{store.data['prefix']}badword add|remove|list <mot>`", delete_after=10)



@bot.command(name='ban')
@is_owner()
async def ban(ctx, member: discord.Member, *, reason=None):
    error = action_error(ctx, member)
    if error:
        return await ctx.send(error, delete_after=10)
    await member.ban(reason=reason)
    await ctx.send(f'{member.mention} a été banni pour : {reason}', delete_after=5)


@bot.command(name='unban')
@is_owner()
async def unban(ctx, user: discord.User, *, reason=None):
    if user.id in store.guild(ctx.guild.id)["blacklist"]:
        return await ctx.send(f"{user.mention} est blacklisté : utilisez `{store.data['prefix']}unbl`.", delete_after=10)
    await ctx.guild.unban(user, reason=reason)
    await ctx.send(f'{user.mention} a été débanni pour : {reason}', delete_after=5)


@bot.command(name='kick')
@is_owner()
async def kick(ctx, member: discord.Member, *, reason=None):
    error = action_error(ctx, member)
    if error:
        return await ctx.send(error, delete_after=10)
    await member.kick(reason=reason)
    await ctx.send(f'{member.mention} a été kické pour : {reason}')


@bot.command(name='mute')
@is_owner()
async def mute(ctx, member: discord.Member, duration: str = None, *, reason=None):
    error = action_error(ctx, member)
    if error:
        return await ctx.send(error, delete_after=10)

    delta = parse_duration(duration)
    if delta is None:
        reason = " ".join(part for part in (duration, reason) if part) or None
        delta = MAX_TIMEOUT
    delta = min(delta, MAX_TIMEOUT)

    await member.timeout(delta, reason=reason)
    await ctx.send(f'{member.mention} a été mute {format_duration(delta)} pour : {reason}')


@bot.command(name='unmute')
@is_owner()
async def unmute(ctx, member: discord.Member, *, reason=None):
    mute_role = discord.utils.get(ctx.guild.roles, name='Muted')
    was_muted = member.is_timed_out() or (mute_role in member.roles)
    if not was_muted:
        return await ctx.send(f"{member.mention} n'est pas mute.")
    if member.is_timed_out():
        await member.timeout(None, reason=reason)
    if mute_role in member.roles:
        await member.remove_roles(mute_role, reason=reason)
    await ctx.send(f'{member.mention} a été unmute.')


@bot.command(name='warn')
@is_owner()
async def warn(ctx, member: discord.Member, *, reason=None):
    error = action_error(ctx, member)
    if error:
        return await ctx.send(error, delete_after=10)

    cfg = store.guild(ctx.guild.id)
    warns = cfg["warns"].setdefault(str(member.id), [])
    warns.append({"reason": reason or "Aucune raison", "by": ctx.author.id, "at": int(time.time())})
    store.save()

    await ctx.send(f'{member.mention}, vous avez été averti pour : {reason} (avertissement n°{len(warns)})')
    embed = log_embed("⚠️ Avertissement", f"{member.mention} a été averti par {ctx.author.mention}.", discord.Color.gold())
    embed.add_field(name="Raison", value=cut(reason or "Aucune raison"), inline=False)
    embed.add_field(name="Total", value=str(len(warns)))
    await send_log(ctx.guild, "mod", embed)

    threshold, action = cfg["warn_threshold"], cfg["warn_action"]
    if action == "none" or threshold <= 0 or len(warns) < threshold:
        return

    auto_reason = f"{len(warns)} avertissements"
    if action == "timeout":
        minutes = cfg["warn_timeout_minutes"]
        await member.timeout(timedelta(minutes=minutes), reason=auto_reason)
        await ctx.send(f"{member.mention} a atteint {len(warns)} avertissements : mute {minutes} min.")
    elif action == "kick":
        await member.kick(reason=auto_reason)
        await ctx.send(f"{member.mention} a atteint {len(warns)} avertissements : expulsé.")
    elif action == "ban":
        await member.ban(reason=auto_reason)
        await ctx.send(f"{member.mention} a atteint {len(warns)} avertissements : banni.")


@bot.command(name='warns')
@is_owner()
async def warns_list(ctx, member: discord.Member):
    warns = store.guild(ctx.guild.id)["warns"].get(str(member.id), [])
    embed = discord.Embed(title=f"Avertissements de {member.display_name} ({len(warns)})", color=COLOR)
    if not warns:
        embed.description = "Aucun avertissement."
    for index, item in enumerate(warns[-20:], start=max(1, len(warns) - 19)):
        embed.add_field(
            name=f"n°{index} — <t:{item['at']}:d>",
            value=f"{cut(item['reason'], 900)}\npar <@{item['by']}>", inline=False)
    await ctx.send(embed=embed)


@bot.command(name='unwarn')
@is_owner()
async def unwarn(ctx, member: discord.Member, number: int = None):
    cfg = store.guild(ctx.guild.id)
    warns = cfg["warns"].get(str(member.id), [])
    if not warns:
        return await ctx.send(f"{member.mention} n'a aucun avertissement.")
    index = len(warns) if number is None else number
    if not 1 <= index <= len(warns):
        return await ctx.send(f"Numéro invalide (1 à {len(warns)}).")
    warns.pop(index - 1)
    if not warns:
        cfg["warns"].pop(str(member.id), None)
    store.save()
    await ctx.send(f"Avertissement n°{index} de {member.mention} retiré.")


@bot.command(name='clearwarns')
@is_owner()
async def clearwarns(ctx, member: discord.Member):
    store.guild(ctx.guild.id)["warns"].pop(str(member.id), None)
    store.save()
    await ctx.send(f"Les avertissements de {member.mention} ont été effacés.")


@bot.command(name='clear')
@is_owner()
async def clear(ctx, amount: int):
    amount = max(1, min(amount, 500))
    deleted = await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f'{max(len(deleted) - 1, 0)} messages ont été supprimés !', delete_after=5)



async def set_lock(channel, locked):
    role = channel.guild.default_role
    overwrite = channel.overwrites_for(role)
    overwrite.send_messages = False if locked else None
    await channel.set_permissions(role, overwrite=overwrite)


@bot.command(name='lock')
@is_owner()
async def lock(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    await set_lock(channel, True)
    await ctx.send(f'{channel.mention} a été verrouillé.')


@bot.command(name='unlock')
@is_owner()
async def unlock(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    await set_lock(channel, False)
    await ctx.send(f'{channel.mention} a été déverrouillé.')


async def lock_everything(ctx, locked):
    count = 0
    for channel in ctx.guild.text_channels:
        try:
            await set_lock(channel, locked)
            count += 1
        except discord.HTTPException:
            pass
    return count


@bot.command(name='lockall')
@is_owner()
async def lockall(ctx):
    count = await lock_everything(ctx, True)
    await ctx.send(f"🔒 {count} salons verrouillés.")


@bot.command(name='unlockall')
@is_owner()
async def unlockall(ctx):
    count = await lock_everything(ctx, False)
    await ctx.send(f"🔓 {count} salons déverrouillés.")


@bot.command(name='slowmode')
@is_owner()
async def slowmode(ctx, seconds: int):
    seconds = max(0, min(seconds, 21600))
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(f"Mode lent réglé sur {seconds} s." if seconds else "Mode lent désactivé.")


@bot.command(name='renew')
@is_owner()
async def renew(ctx):
    new_channel = await ctx.channel.clone(reason='Salon renouvelé')
    await new_channel.edit(position=ctx.channel.position)
    await ctx.channel.delete()
    await new_channel.send(f'{ctx.author.mention} a renouvelé le salon.', delete_after=5)


@bot.command(name='delete_channel')
@is_owner()
async def delete_channel(ctx, channel_id: int):
    channel = ctx.guild.get_channel(channel_id)
    if channel is None:
        return await ctx.send(f'Erreur: Le salon avec l\'ID `{channel_id}` n\'existe pas.', delete_after=5)
    await channel.delete()
    if channel.id != ctx.channel.id:
        await ctx.send(f'Le salon `{channel.name}` a été supprimé avec succès!', delete_after=5)


def manageable_roles(guild):
    top = guild.me.top_role
    return [r for r in guild.roles if not r.is_default() and not r.managed and r < top]


@bot.command(name='rank')
@is_owner()
async def rank(ctx, member: discord.Member):
    roles = [r for r in manageable_roles(ctx.guild) if r not in member.roles]
    if roles:
        await member.add_roles(*roles, reason=f"rank par {ctx.author}")
    await ctx.send(f'{member.mention} a été gradé ! ({len(roles)} rôles ajoutés)')


@bot.command(name='derank')
@is_owner()
async def derank(ctx, member: discord.Member):
    roles = [r for r in manageable_roles(ctx.guild) if r in member.roles]
    if roles:
        await member.remove_roles(*roles, reason=f"derank par {ctx.author}")
    await ctx.send(f'{member.mention} a été rétrogradé ! ({len(roles)} rôles retirés)')


@bot.command(name='nick')
@is_owner()
async def nickname(ctx, member: discord.Member, *, nickname: str):
    await member.edit(nick=nickname[:32])
    await ctx.send(f'Le pseudo de {member.mention} a été changé en {nickname[:32]}.', delete_after=5)


@bot.command(name="addemoji")
@is_owner()
async def addemoji(ctx, emoji: str, *, name: str = None):
    """Ajoute un emoji au serveur à partir de son ID ou de l'emoji lui-même."""
    match = re.match(r"<(a?):(\w+):(\d+)>$", emoji)
    if match:
        emoji_id, name = match.group(3), name or match.group(2)
    elif emoji.isdigit():
        emoji_id = emoji
    else:
        return await ctx.send("🚫 Erreur : donnez un emoji personnalisé ou son ID.", delete_after=5)
    if not name:
        return await ctx.send("🚫 Erreur : un nom est requis quand on utilise un ID.", delete_after=5)

    image_data = None
    async with aiohttp.ClientSession() as session:
        for ext in ("gif", "png"):
            async with session.get(f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}") as response:
                if response.status == 200:
                    image_data = await response.read()
                    break
    if image_data is None:
        return await ctx.send("🚫 Erreur : Impossible de récupérer l'emoji. Vérifie l'ID.", delete_after=5)

    try:
        created = await ctx.guild.create_custom_emoji(name=name, image=image_data)
        await ctx.send(f"✅ Emoji `{name}` ajouté avec succès : {created}", delete_after=5)
    except discord.HTTPException as e:
        await ctx.send(f"🚫 Erreur : Impossible d'ajouter l'emoji.\n```{e}```", delete_after=5)


@bot.command(name='logs')
@is_owner()
async def logs(ctx):
    guild = ctx.guild
    cfg = store.guild(guild.id)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
        ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
    }

    created = []
    category = None
    for key, name in (("log_messages", "logs-messages"), ("log_voice", "logs-vocal"), ("log_mod", "logs-gestion")):
        if cfg[key] and guild.get_channel(cfg[key]):
            continue
        if category is None:
            category = discord.utils.get(guild.categories, name="Logs") or await guild.create_category("Logs", overwrites=overwrites)
        channel = await guild.create_text_channel(name, overwrites=overwrites, category=category)
        cfg[key] = channel.id
        created.append(channel.mention)
    store.save()

    if created:
        await ctx.send(f"✅ Salons de logs créés : {', '.join(created)}", delete_after=10)
    else:
        await ctx.send("Les salons de logs existent déjà.", delete_after=5)



async def set_join_leave(ctx, kind, label, channel, message):
    cfg = store.guild(ctx.guild.id)
    if channel is None:
        cfg[f"{kind}_enabled"] = False
        store.save()
        return await ctx.send(f"Message {label} désactivé.")
    cfg[f"{kind}_enabled"] = True
    cfg[f"{kind}_channel"] = channel.id
    if message:
        cfg[f"{kind}_message"] = message[:1500]
    store.save()
    await ctx.send(f"✅ Message {label} activé dans {channel.mention}.")


@bot.command(name='welcome')
@is_owner()
async def welcome(ctx, channel: discord.TextChannel = None, *, message: str = None):
    await set_join_leave(ctx, "welcome", "de bienvenue", channel, message)


@bot.command(name='leave')
@is_owner()
async def leave(ctx, channel: discord.TextChannel = None, *, message: str = None):
    await set_join_leave(ctx, "leave", "de départ", channel, message)


@bot.command(name='autorole')
@is_owner()
async def autorole(ctx, role: discord.Role = None):
    cfg = store.guild(ctx.guild.id)
    if role is None:
        cfg["autorole"] = None
        store.save()
        return await ctx.send("Rôle automatique désactivé.")
    if role.managed or role >= ctx.guild.me.top_role:
        return await ctx.send("Je ne peux pas attribuer ce rôle (il est au-dessus du mien ou géré par une intégration).")
    cfg["autorole"] = role.id
    store.save()
    await ctx.send(f"✅ Les nouveaux membres recevront le rôle **{role.name}**.")


async def draw_winner(message):
    reaction = discord.utils.get(message.reactions, emoji="🎉")
    users = [user async for user in reaction.users() if not user.bot] if reaction else []
    return random.choice(users) if users else None


async def run_giveaway(gw):
    await bot.wait_until_ready()
    await asyncio.sleep(max(0, gw["end"] - time.time()))

    if gw in store.data["giveaways"]:
        store.data["giveaways"].remove(gw)
        store.save()

    channel = bot.get_channel(gw["channel"])
    if channel is None:
        return
    try:
        message = await channel.fetch_message(gw["message"])
        winner = await draw_winner(message)
        if winner:
            await channel.send(
                f"🎉 Félicitations {winner.mention}, vous avez gagné **{gw['prize']}**! "
                "Contactez le owner en DM pour récupérer votre prix.", reference=message)
        else:
            await channel.send("Personne n'a participé au giveaway.", reference=message)
        embed = message.embeds[0]
        embed.set_footer(text="Terminé")
        await message.edit(embed=embed)
    except discord.HTTPException:
        pass


@bot.command(name='giveaway')
@is_owner()
async def giveaway(ctx, duration: str, *, prize: str):
    delta = parse_duration(duration)
    if delta is None:
        return await ctx.send(
            f"🚫 Erreur : durée invalide.\nExemples : `{store.data['prefix']}giveaway 60 Nitro` (minutes), `30m`, `2h`, `1j`")

    end_time = discord.utils.utcnow() + delta
    embed = discord.Embed(
        title="🎉 **GIVEAWAY** 🎉",
        description=f"Réagissez avec 🎉 pour participer!\n**Prix:** {prize}\n**Fin:** {discord.utils.format_dt(end_time, style='R')}",
        color=COLOR,
    )
    embed.set_footer(text="Fin")
    embed.timestamp = end_time

    message = await ctx.send(embed=embed)
    await message.add_reaction("🎉")

    gw = {"channel": ctx.channel.id, "message": message.id, "prize": prize, "end": end_time.timestamp()}
    store.data["giveaways"].append(gw)
    store.save()
    spawn(run_giveaway(gw))


@bot.command(name='reroll')
@is_owner()
async def reroll(ctx, message_id: int):
    try:
        message = await ctx.channel.fetch_message(message_id)
    except discord.HTTPException:
        return await ctx.send("Message introuvable dans ce salon.", delete_after=5)
    winner = await draw_winner(message)
    if winner:
        await ctx.send(f"🎉 Nouveau gagnant : {winner.mention} !")
    else:
        await ctx.send("Aucun participant sur ce message.")


@bot.command(name='poll')
@is_owner()
async def poll(ctx, *, text: str):
    parts = [part.strip() for part in text.split("|") if part.strip()]
    question, options = parts[0], parts[1:10]
    numbers = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]

    embed = discord.Embed(title=f"📊 {cut(question, 250)}", color=COLOR)
    if len(options) >= 2:
        embed.description = "\n".join(f"{numbers[i]} {option}" for i, option in enumerate(options))
        emojis = numbers[:len(options)]
    else:
        emojis = ["👍", "👎"]
    embed.set_footer(text=f"Sondage de {ctx.author.display_name}")

    await safe_delete(ctx.message)
    message = await ctx.send(embed=embed)
    for emoji in emojis:
        await message.add_reaction(emoji)


@bot.command(name='say')
@is_owner()
async def say(ctx, *, text: str):
    await safe_delete(ctx.message)
    await ctx.send(text)


class EmbedModal(Modal):
    def __init__(self, user, channel):
        super().__init__(title="Créer un Embed")
        self.user = user
        self.channel = channel
        self.add_item(TextInput(label="Titre", placeholder="Entrez le titre", max_length=256))
        self.add_item(TextInput(label="Description", placeholder="Entrez la description", style=discord.TextStyle.long))
        self.add_item(TextInput(label="Couleur", placeholder="Ex: blue, red, green... ou #ff00aa", required=False))
        self.add_item(TextInput(label="Image URL", placeholder="URL d'image (optionnel)", required=False))

    async def on_submit(self, interaction: discord.Interaction):
        colors = {
            "blue": discord.Color.blue(),
            "red": discord.Color.red(),
            "green": discord.Color.green(),
            "yellow": discord.Color.gold(),
            "purple": discord.Color.purple(),
            "pink": discord.Color.magenta()
        }

        raw_color = self.children[2].value.lower().strip()
        embed_color = colors.get(raw_color, discord.Color.blue())
        if re.fullmatch(r"#?[0-9a-f]{6}", raw_color):
            embed_color = discord.Color(int(raw_color.lstrip("#"), 16))

        embed = discord.Embed(title=self.children[0].value, description=self.children[1].value, color=embed_color)
        if self.children[3].value.startswith(("http://", "https://")):
            embed.set_image(url=self.children[3].value)
        embed.set_footer(text=f"Créé par {self.user.name}", icon_url=self.user.display_avatar.url)

        await self.channel.send(embed=embed)
        await interaction.response.send_message("Embed envoyé dans le salon !", ephemeral=True)


class EmbedView(View):
    def __init__(self, user, channel):
        super().__init__(timeout=180)
        self.user = user
        self.channel = channel

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Ce bouton n'est pas le vôtre.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Créer un Embed", style=discord.ButtonStyle.primary)
    async def create_embed(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(EmbedModal(self.user, self.channel))


@bot.command(name='embed')
@is_owner()
async def send_embed(ctx):
    """
    Commande pour créer un embed personnalisé avec un formulaire interactif.
    """
    view = EmbedView(ctx.author, ctx.channel)
    await ctx.send("Cliquez sur le bouton ci-dessous pour créer un embed.", view=view, delete_after=180)



@bot.command(name='serverinfo')
async def serverinfo(ctx):
    guild = ctx.guild
    embed = discord.Embed(title=f"Informations sur le serveur : {guild.name}", color=COLOR)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(name="Nom", value=guild.name, inline=True)
    embed.add_field(name="ID", value=guild.id, inline=True)
    embed.add_field(name="Propriétaire", value=f"<@{guild.owner_id}>", inline=True)
    embed.add_field(name="Membres", value=guild.member_count, inline=True)
    embed.add_field(name="Rôles", value=len(guild.roles), inline=True)
    embed.add_field(name="Salons", value=len(guild.channels), inline=True)
    embed.add_field(name="Boosts", value=f"{guild.premium_subscription_count} (niveau {guild.premium_tier})", inline=True)
    embed.add_field(name="Emojis", value=len(guild.emojis), inline=True)
    embed.add_field(name="Langue", value=str(guild.preferred_locale), inline=True)
    embed.set_footer(text=f"Créé le : {guild.created_at.strftime('%d/%m/%Y à %H:%M:%S')}")

    await ctx.send(embed=embed)


@bot.command(name='userinfo')
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    roles = [role for role in reversed(member.roles) if not role.is_default()]
    warns = store.guild(ctx.guild.id)["warns"].get(str(member.id), [])

    embed = discord.Embed(
        title=f"Informations sur l'utilisateur : {member}",
        color=COLOR,
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Nom", value=member.display_name, inline=True)
    embed.add_field(name="ID", value=member.id, inline=True)
    embed.add_field(name="Rôle le plus élevé", value=member.top_role.mention, inline=True)
    embed.add_field(name="Rejoint le", value=discord.utils.format_dt(member.joined_at, style='f'), inline=True)
    embed.add_field(name="Créé le", value=discord.utils.format_dt(member.created_at, style='f'), inline=True)
    embed.add_field(name="Avertissements", value=len(warns), inline=True)
    embed.add_field(
        name=f"Rôles ({len(roles)})",
        value=cut(" ".join(role.mention for role in roles)) or "Aucun rôle",
        inline=False
    )
    await ctx.send(embed=embed)


@bot.command(name='avatar')
async def avatar(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"Avatar de {member.display_name}", color=COLOR)
    embed.set_image(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name='snipe')
@is_owner()
async def snipe(ctx):
    data = snipes.get(ctx.channel.id)
    if not data:
        return await ctx.send("Aucun message supprimé récemment dans ce salon.", delete_after=5)
    author, content, when = data
    embed = discord.Embed(description=cut(content, 4000) or "*Aucun contenu*", color=COLOR, timestamp=when)
    embed.set_author(name=author)
    await ctx.send(embed=embed)


@bot.command(name='ping')
async def ping(ctx):
    uptime = timedelta(seconds=int(time.time() - bot.start_time))
    await ctx.send(f"🏓 Latence : {round(bot.latency * 1000)} ms — en ligne depuis {uptime}")


@bot.command(name='config')
@is_owner()
async def show_config(ctx):
    cfg = store.guild(ctx.guild.id)

    def flag(key):
        return "✅" if cfg[key] else "❌"

    def channel(key):
        return f"<#{cfg[key]}>" if cfg[key] else "non défini"

    autorole_text = f"<@&{cfg['autorole']}>" if cfg["autorole"] else "aucun"
    embed = discord.Embed(title=f"Configuration — {ctx.guild.name}", color=COLOR)
    embed.add_field(name="Protection", inline=False, value=(
        f"{flag('antilink')} Anti-liens\n{flag('antieveryone')} Anti everyone/here\n"
        f"{flag('antispam')} Anti-spam ({cfg['spam_messages']} msg / {cfg['spam_seconds']} s)\n"
        f"{flag('antiraid')} Anti-raid ({cfg['raid_joins']} arrivées / {cfg['raid_seconds']} s)\n"
        f"Mots interdits : {len(cfg['badwords'])}"))
    embed.add_field(name="Communauté", inline=False, value=(
        f"{flag('welcome_enabled')} Bienvenue : {channel('welcome_channel')}\n"
        f"{flag('leave_enabled')} Départ : {channel('leave_channel')}\n"
        f"Rôle automatique : {autorole_text}"))
    embed.add_field(name="Logs", inline=False, value=(
        f"Messages : {channel('log_messages')}\nVocal : {channel('log_voice')}\nGestion : {channel('log_mod')}"))
    embed.add_field(name="Sanctions", inline=False, value=(
        f"{cfg['warn_threshold']} avertissements → {cfg['warn_action']}\n"
        f"Whitelist : {len(cfg['whitelist'])} — Blacklist : {len(cfg['blacklist'])}"))
    await ctx.send(embed=embed)


@bot.command(name='prefix')
@is_owner()
async def set_prefix(ctx, prefix: str):
    store.data["prefix"] = prefix[:5]
    store.save()
    await ctx.send(f"✅ Préfixe changé en `{prefix[:5]}`.")


@bot.command(name="setstatus")
@is_owner()
async def setstatus(ctx, status: str):
    status = status.lower()
    if status not in ("online", "idle", "dnd", "invisible"):
        return await ctx.send("🚫 Erreur : Statut invalide. Utilisez `online`, `idle`, `dnd`, ou `invisible`.", delete_after=5)

    store.data["status"] = status
    store.save()
    await apply_presence()
    await ctx.send(f"✅ Statut du bot changé en `{status}` !", delete_after=5)


@bot.command(name='setactivity')
@is_owner()
async def setactivity(ctx, *, activity: str = ""):
    store.data["activity"] = activity[:120]
    store.save()
    await apply_presence()
    await ctx.send(f"L'activité du bot a été mise à jour : {activity or 'aucune'}", delete_after=5)


if not config["token"]:
    raise SystemExit(f"Aucun token : renseignez \"token\" dans {CONFIG_PATH.name} ou la variable DISCORD_TOKEN.")

bot.run(config["token"])
