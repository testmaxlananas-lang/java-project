import discord
import aiohttp
import asyncio
import os
import json
import subprocess
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timezone

TOKEN      = os.environ["DISCORD_BOT_TOKEN"]
GUILD      = int(os.environ["DISCORD_GUILD_ID"])
CHANNEL    = int(os.environ["DISCORD_CHANNEL_ID"])
PAT        = os.environ["GITHUB_PAT"]
REPO       = os.environ["GITHUB_REPO"]
EVENT_FILE = os.environ.get("MC_EVENT_FILE", "/tmp/mc_events.jsonl")
MC_PIPE    = os.environ.get("MC_PIPE", "/tmp/mc_input")
STATS_FILE = os.environ.get("MC_STATS_FILE", "/tmp/mc_stats.txt")
MC_LOG     = os.environ.get("MC_LOG", "mc.log")

intents                 = discord.Intents.default()
intents.message_content = True
bot                     = commands.Bot(command_prefix="!", intents=intents)
tree                    = bot.tree

msg_queue: asyncio.Queue = asyncio.Queue()


# ── Envoi commande MC ────────────────────────────────────────────────────────
def mc_cmd(cmd: str):
    try:
        with open(MC_PIPE, "w") as f:
            f.write(cmd + "\n")
            f.flush()
        return True
    except Exception as e:
        print(f"[Bot] mc_cmd erreur: {e}", flush=True)
        return False


# ── Lecture joueurs connectes depuis mc.log ──────────────────────────────────
def get_online_players() -> list[str]:
    if not os.path.isfile(MC_LOG):
        return []
    joined  = []
    left    = set()
    try:
        with open(MC_LOG, "r", errors="replace") as f:
            for line in f:
                if "logged in with entity id" in line:
                    import re
                    m = re.search(r'INFO\]:\s+(\w+)\[/', line)
                    if m:
                        joined.append(m.group(1))
                elif "lost connection:" in line:
                    import re
                    m = re.search(r'INFO\]:\s+(\w+)\s+lost connection', line)
                    if m:
                        left.add(m.group(1))
    except Exception:
        pass
    seen    = set()
    online  = []
    for p in reversed(joined):
        if p not in seen and p not in left:
            seen.add(p)
            online.append(p)
    return list(reversed(online))


# ── Lecture stats systeme ────────────────────────────────────────────────────
def get_stats() -> dict:
    result = {}
    if not os.path.isfile(STATS_FILE):
        return result
    try:
        with open(STATS_FILE) as f:
            line = f.read().strip()
        for part in line.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                result[k] = v
    except Exception:
        pass
    return result


# ── Lecture events MC ────────────────────────────────────────────────────────
async def event_reader():
    loop   = asyncio.get_event_loop()
    waited = 0
    while not os.path.isfile(EVENT_FILE):
        await asyncio.sleep(1)
        waited += 1
        if waited > 120:
            return

    print(f"[Bot] Lecture events: {EVENT_FILE}", flush=True)

    def read_lines(pos):
        lines = []
        try:
            with open(EVENT_FILE, "r", errors="replace") as f:
                f.seek(pos)
                for line in f:
                    lines.append(line.rstrip())
                new_pos = f.tell()
        except Exception:
            new_pos = pos
        return lines, new_pos

    pos = os.path.getsize(EVENT_FILE) if os.path.isfile(EVENT_FILE) else 0

    while True:
        await asyncio.sleep(0.3)
        try:
            size = os.path.getsize(EVENT_FILE)
        except Exception:
            continue
        if size < pos:
            pos = 0
        if size == pos:
            continue
        lines, pos = await loop.run_in_executor(None, read_lines, pos)
        for raw in lines:
            if not raw.strip():
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = format_event(ev)
            if msg:
                await msg_queue.put(msg)


def format_event(ev: dict) -> str | None:
    t = ev.get("type", "")
    if t == "start":
        jvm = ev.get("jvm", "?")
        cpu = ev.get("cpus", "?")
        return f":white_check_mark: **Le serveur est en ligne !** JVM: `{jvm}` | CPUs: `{cpu}`"
    if t == "stop":
        return ":octagonal_sign: **Le serveur est arrete.**"
    if t == "restart":
        mins = ev.get("minutes", "?")
        return f":arrows_counterclockwise: **Redemarrage automatique** apres `{mins}` minutes."
    if t == "crash":
        n     = ev.get("count", "?")
        cause = ev.get("cause", "Inconnue")
        return f":red_circle: **Crash #{n}** — `{cause}` — Redemarrage dans 10s..."
    if t == "crash_fatal":
        return ":skull: **Crash fatal** — 5 crashs consecutifs. Arret definitif."
    if t == "join":
        return f":arrow_right: **{ev.get('player','?')}** a rejoint le serveur."
    if t == "quit":
        return f":arrow_left: **{ev.get('player','?')}** a quitte le serveur."
    if t == "chat":
        return f"**{ev.get('player','?')}** {ev.get('text','')}"
    if t == "death":
        return f":skull_crossbones: {ev.get('text','')}"
    return None


# ── Flush queue → Discord ────────────────────────────────────────────────────
@tasks.loop(seconds=0.5)
async def flush_queue():
    channel = bot.get_channel(CHANNEL)
    if channel is None:
        return
    batch = []
    while not msg_queue.empty() and len(batch) < 5:
        batch.append(await msg_queue.get())
    if batch:
        try:
            await channel.send("\n".join(batch))
        except Exception as e:
            print(f"[Bot] Send erreur: {e}", flush=True)


# ════════════════════════════════════════════════════════════════════════════
#  COMMANDES PREFIX
# ════════════════════════════════════════════════════════════════════════════

@bot.command(name="players", aliases=["pl", "list", "who"])
async def cmd_players(ctx):
    """Liste les joueurs connectes."""
    players = get_online_players()
    if not players:
        await ctx.send(":red_circle: Aucun joueur connecte.")
    else:
        names = ", ".join(f"**{p}**" for p in players)
        await ctx.send(f":green_circle: **{len(players)} joueur(s) en ligne:** {names}")


@bot.command(name="kick")
async def cmd_kick(ctx, player: str = None, *, reason: str = "Kicked by admin"):
    """Kick un joueur: !kick <pseudo> [raison]"""
    if player is None:
        await ctx.send(":x: Usage: `!kick <pseudo> [raison]`")
        return
    ok = mc_cmd(f"kick {player} {reason}")
    if ok:
        await ctx.send(f":boot: **{player}** a ete kick. Raison: `{reason}`")
    else:
        await ctx.send(":x: Impossible d'envoyer la commande au serveur.")


@bot.command(name="ban")
async def cmd_ban(ctx, player: str = None, *, reason: str = "Banned by admin"):
    """Ban un joueur: !ban <pseudo> [raison]"""
    if player is None:
        await ctx.send(":x: Usage: `!ban <pseudo> [raison]`")
        return
    mc_cmd(f"ban {player} {reason}")
    await ctx.send(f":hammer: **{player}** a ete banni. Raison: `{reason}`")


@bot.command(name="unban", aliases=["pardon"])
async def cmd_unban(ctx, player: str = None):
    """Unban un joueur: !unban <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!unban <pseudo>`")
        return
    mc_cmd(f"pardon {player}")
    await ctx.send(f":white_check_mark: **{player}** a ete debanni.")


@bot.command(name="op")
async def cmd_op(ctx, player: str = None):
    """Donne les droits op: !op <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!op <pseudo>`")
        return
    mc_cmd(f"op {player}")
    await ctx.send(f":crown: **{player}** est maintenant OP.")


@bot.command(name="deop")
async def cmd_deop(ctx, player: str = None):
    """Retire les droits op: !deop <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!deop <pseudo>`")
        return
    mc_cmd(f"deop {player}")
    await ctx.send(f":no_entry: **{player}** n'est plus OP.")


@bot.command(name="say")
async def cmd_say(ctx, *, message: str = None):
    """Envoie un message dans le chat MC: !say <message>"""
    if message is None:
        await ctx.send(":x: Usage: `!say <message>`")
        return
    mc_cmd(f"say [Discord] {message}")
    await ctx.send(f":speech_balloon: Message envoye: `{message}`")


@bot.command(name="tp")
async def cmd_tp(ctx, player: str = None, target: str = None):
    """Teleporte un joueur: !tp <joueur> <destination>"""
    if player is None or target is None:
        await ctx.send(":x: Usage: `!tp <joueur> <destination>`")
        return
    mc_cmd(f"tp {player} {target}")
    await ctx.send(f":zap: **{player}** teleporte vers **{target}**.")


@bot.command(name="give")
async def cmd_give(ctx, player: str = None, item: str = None, amount: str = "1"):
    """Donne un item: !give <joueur> <item> [quantite]"""
    if player is None or item is None:
        await ctx.send(":x: Usage: `!give <joueur> <item> [quantite]`")
        return
    mc_cmd(f"give {player} {item} {amount}")
    await ctx.send(f":gift: Donne `{amount}x {item}` a **{player}**.")


@bot.command(name="gamemode", aliases=["gm"])
async def cmd_gamemode(ctx, player: str = None, mode: str = None):
    """Change le gamemode: !gamemode <joueur> <creative|survival|spectator|adventure>"""
    modes = {"c": "creative", "s": "survival", "sp": "spectator", "a": "adventure",
             "creative": "creative", "survival": "survival",
             "spectator": "spectator", "adventure": "adventure",
             "0": "survival", "1": "creative", "2": "adventure", "3": "spectator"}
    if player is None or mode is None:
        await ctx.send(":x: Usage: `!gamemode <joueur> <creative|survival|spectator|adventure>`")
        return
    resolved = modes.get(mode.lower(), mode)
    mc_cmd(f"gamemode {resolved} {player}")
    await ctx.send(f":joystick: Gamemode de **{player}** -> `{resolved}`.")


@bot.command(name="time")
async def cmd_time(ctx, value: str = None):
    """Change l'heure: !time <day|night|noon|midnight|0-24000>"""
    if value is None:
        await ctx.send(":x: Usage: `!time <day|night|noon|midnight|0-24000>`")
        return
    mc_cmd(f"time set {value}")
    await ctx.send(f":clock3: Heure changee -> `{value}`.")


@bot.command(name="weather")
async def cmd_weather(ctx, value: str = None):
    """Change la meteo: !weather <clear|rain|thunder>"""
    if value is None:
        await ctx.send(":x: Usage: `!weather <clear|rain|thunder>`")
        return
    mc_cmd(f"weather {value}")
    await ctx.send(f":cloud: Meteo -> `{value}`.")


@bot.command(name="difficulty")
async def cmd_difficulty(ctx, value: str = None):
    """Change la difficulte: !difficulty <peaceful|easy|normal|hard>"""
    if value is None:
        await ctx.send(":x: Usage: `!difficulty <peaceful|easy|normal|hard>`")
        return
    mc_cmd(f"difficulty {value}")
    await ctx.send(f":crossed_swords: Difficulte -> `{value}`.")


@bot.command(name="whitelist", aliases=["wl"])
async def cmd_whitelist(ctx, action: str = None, player: str = None):
    """Gere la whitelist: !whitelist <add|remove|on|off|list> [joueur]"""
    if action is None:
        await ctx.send(":x: Usage: `!whitelist <add|remove|on|off|list> [joueur]`")
        return
    if action in ("add", "remove") and player:
        mc_cmd(f"whitelist {action} {player}")
        await ctx.send(f":notepad_spiral: Whitelist `{action}` -> **{player}**.")
    elif action in ("on", "off", "list"):
        mc_cmd(f"whitelist {action}")
        await ctx.send(f":notepad_spiral: Whitelist -> `{action}`.")
    else:
        await ctx.send(":x: Action invalide.")


@bot.command(name="stop")
async def cmd_stop(ctx):
    """Arrete le serveur: !stop"""
    mc_cmd("stop")
    await ctx.send(":octagonal_sign: Arret du serveur envoye.")


@bot.command(name="restart")
async def cmd_restart_prefix(ctx):
    """Relance le workflow: !restart"""
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/server.yml/dispatches"
    headers = {"Authorization": f"token {PAT}", "Accept": "application/vnd.github.v3+json"}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, json={"ref": "main"}) as r:
            if r.status == 204:
                await ctx.send(":white_check_mark: Serveur relance !")
            else:
                await ctx.send(f":x: Erreur {r.status}")


@bot.command(name="status")
async def cmd_status_prefix(ctx):
    """Statut du serveur: !status"""
    stats   = get_stats()
    players = get_online_players()
    if not stats:
        await ctx.send(":red_circle: **Serveur hors ligne ou en demarrage...**")
        return
    lines = [
        ":green_circle: **Statut du serveur**",
        f"```",
        f"Heure  : {stats.get('TS', 'N/A')}",
        f"CPU    : {stats.get('CPU', 'N/A')}",
        f"RAM    : {stats.get('RAM', 'N/A')}",
        f"Swap   : {stats.get('SWAP', 'N/A')}",
        f"Disque : {stats.get('DISK', 'N/A')}",
        f"TPS    : {stats.get('TPS', 'N/A')}",
        f"Online : {len(players)} joueur(s)",
        f"```",
    ]
    if players:
        lines.append(f":busts_in_silhouette: {', '.join(players)}")
    await ctx.send("\n".join(lines))


@bot.command(name="tps")
async def cmd_tps(ctx):
    """Affiche les TPS: !tps"""
    mc_cmd("tps")
    stats = get_stats()
    tps   = stats.get("TPS", "N/A")
    emoji = ":green_circle:" if tps != "N/A" and float(tps) >= 18 else ":yellow_circle:" if tps != "N/A" and float(tps) >= 15 else ":red_circle:"
    await ctx.send(f"{emoji} **TPS:** `{tps}`")


@bot.command(name="broadcast", aliases=["bc"])
async def cmd_broadcast(ctx, *, message: str = None):
    """Broadcast un message: !broadcast <message>"""
    if message is None:
        await ctx.send(":x: Usage: `!broadcast <message>`")
        return
    mc_cmd(f"broadcast {message}")
    await ctx.send(f":mega: Broadcast: `{message}`")


@bot.command(name="heal")
async def cmd_heal(ctx, player: str = None):
    """Soigne un joueur: !heal <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!heal <pseudo>`")
        return
    mc_cmd(f"heal {player}")
    await ctx.send(f":heart: **{player}** soigne.")


@bot.command(name="fly")
async def cmd_fly(ctx, player: str = None):
    """Toggle le vol: !fly <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!fly <pseudo>`")
        return
    mc_cmd(f"fly {player}")
    await ctx.send(f":airplane: Vol toggle pour **{player}**.")


@bot.command(name="clear")
async def cmd_clear(ctx, player: str = None):
    """Vide l'inventaire: !clear <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!clear <pseudo>`")
        return
    mc_cmd(f"clear {player}")
    await ctx.send(f":wastebasket: Inventaire de **{player}** vide.")


@bot.command(name="sudo")
async def cmd_sudo(ctx, *, command: str = None):
    """Envoie une commande brute au serveur: !sudo <commande>"""
    if command is None:
        await ctx.send(":x: Usage: `!sudo <commande>`")
        return
    mc_cmd(command)
    await ctx.send(f":computer: Commande envoyee: `{command}`")


@bot.command(name="logs")
async def cmd_logs(ctx, lines: int = 20):
    """Affiche les derniers logs MC: !logs [nb_lignes]"""
    if not os.path.isfile(MC_LOG):
        await ctx.send(":x: Fichier log introuvable.")
        return
    lines = min(lines, 40)
    try:
        result = subprocess.run(
            ["tail", f"-{lines}", MC_LOG],
            capture_output=True, text=True, timeout=5
        )
        content = result.stdout.strip()
        if len(content) > 1900:
            content = content[-1900:]
        await ctx.send(f"```\n{content}\n```")
    except Exception as e:
        await ctx.send(f":x: Erreur: {e}")


@bot.command(name="help_mc", aliases=["cmds", "commands"])
async def cmd_help(ctx):
    """Liste toutes les commandes disponibles."""
    help_text = """
:joystick: **Commandes du bot Minecraft**

**Joueurs**
`!players` — Liste des joueurs en ligne
`!status` — Statut complet du serveur
`!tps` — TPS actuel

**Moderation**
`!kick <pseudo> [raison]` — Kick un joueur
`!ban <pseudo> [raison]` — Ban un joueur
`!unban <pseudo>` — Unban un joueur
`!op <pseudo>` — Donner OP
`!deop <pseudo>` — Retirer OP
`!whitelist <add|remove|on|off|list> [pseudo]`

**Joueur**
`!tp <joueur> <destination>` — Teleporter
`!gamemode <joueur> <mode>` — Changer gamemode
`!give <joueur> <item> [quantite]` — Donner item
`!heal <pseudo>` — Soigner
`!fly <pseudo>` — Toggle vol
`!clear <pseudo>` — Vider inventaire

**Monde**
`!time <day|night|noon|midnight>` — Heure
`!weather <clear|rain|thunder>` — Meteo
`!difficulty <peaceful|easy|normal|hard>` — Difficulte

**Serveur**
`!say <message>` — Chat MC
`!broadcast <message>` — Broadcast
`!restart` — Relancer le serveur
`!stop` — Arreter le serveur
`!sudo <commande>` — Commande brute
`!logs [nb]` — Derniers logs
"""
    await ctx.send(help_text)


# ════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="status", description="Statut du serveur", guild=discord.Object(id=GUILD))
async def slash_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    stats   = get_stats()
    players = get_online_players()
    if not stats:
        await interaction.followup.send(":red_circle: **Hors ligne**", ephemeral=True)
        return
    lines = [
        f"**CPU:** {stats.get('CPU','N/A')} | **RAM:** {stats.get('RAM','N/A')}",
        f"**TPS:** {stats.get('TPS','N/A')} | **Disque:** {stats.get('DISK','N/A')}",
        f"**Joueurs:** {len(players)} — {', '.join(players) if players else 'aucun'}",
    ]
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@tree.command(name="restart", description="Relancer le serveur", guild=discord.Object(id=GUILD))
async def slash_restart(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    url     = f"https://api.github.com/repos/{REPO}/actions/workflows/server.yml/dispatches"
    headers = {"Authorization": f"token {PAT}", "Accept": "application/vnd.github.v3+json"}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, json={"ref": "main"}) as r:
            msg = ":white_check_mark: Relance!" if r.status == 204 else f":x: Erreur {r.status}"
    await interaction.followup.send(msg, ephemeral=True)


# ── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD))
    print(f"[Bot] Connecte: {bot.user}", flush=True)
    flush_queue.start()
    asyncio.create_task(event_reader())


bot.run(TOKEN)
