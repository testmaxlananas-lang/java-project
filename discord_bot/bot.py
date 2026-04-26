import discord
import aiohttp
import asyncio
import os
import re
import json
import subprocess
from discord.ext import commands, tasks

TOKEN      = os.environ["DISCORD_BOT_TOKEN"]
GUILD      = int(os.environ["DISCORD_GUILD_ID"])
CHANNEL    = int(os.environ["DISCORD_CHANNEL_ID"])
PAT        = os.environ["GITHUB_PAT"]
REPO       = os.environ["GITHUB_REPO"]
EVENT_FILE = os.environ.get("MC_EVENT_FILE",  "/tmp/mc_events.jsonl")
MC_PIPE    = os.environ.get("MC_PIPE",        "/tmp/mc_input")
STATS_FILE = os.environ.get("MC_STATS_FILE",  "/tmp/mc_stats.txt")
MC_LOG     = os.environ.get("MC_LOG",         "/home/runner/work/java-project/java-project/mc.log")

intents                 = discord.Intents.default()
intents.message_content = True
bot                     = commands.Bot(command_prefix="!", intents=intents)

msg_queue: asyncio.Queue = asyncio.Queue()


# ── Envoi commande au serveur MC ─────────────────────────────────────────────
def mc_cmd(cmd: str) -> bool:
    try:
        with open(MC_PIPE, "w") as f:
            f.write(cmd + "\n")
            f.flush()
        return True
    except Exception as e:
        print(f"[Bot] mc_cmd erreur: {e}", flush=True)
        return False


# ── Joueurs en ligne : suivi par events jsonl (fiable) ───────────────────────
_online_players: set[str] = set()

def get_online_players() -> list[str]:
    return sorted(_online_players)


# ── Stats systeme ─────────────────────────────────────────────────────────────
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


# ── Lecture events MC → queue Discord ────────────────────────────────────────
async def event_reader():
    global _online_players
    loop   = asyncio.get_event_loop()
    waited = 0
    while not os.path.isfile(EVENT_FILE):
        await asyncio.sleep(1)
        waited += 1
        if waited > 120:
            print("[Bot] Timeout attente EVENT_FILE", flush=True)
            return

    print(f"[Bot] Lecture events: {EVENT_FILE}", flush=True)

    def read_lines(pos: int):
        lines   = []
        new_pos = pos
        try:
            with open(EVENT_FILE, "r", errors="replace") as f:
                f.seek(pos)
                for line in f:
                    lines.append(line.rstrip())
                new_pos = f.tell()
        except Exception:
            pass
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
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Suivi joueurs en ligne
            t = ev.get("type", "")
            if t == "join":
                _online_players.add(ev.get("player", ""))
            elif t == "quit":
                _online_players.discard(ev.get("player", ""))
            elif t == "start":
                _online_players.clear()

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
        return f":arrows_counterclockwise: **Redemarrage automatique** apres `{ev.get('minutes','?')}` minutes."
    if t == "crash":
        return f":red_circle: **Crash #{ev.get('count','?')}** — `{ev.get('cause','?')}` — Redemarrage dans 10s..."
    if t == "crash_fatal":
        return ":skull: **Crash fatal** — Arret definitif."
    if t == "join":
        return f":arrow_right: **{ev.get('player','?')}** a rejoint le serveur."
    if t == "quit":
        return f":arrow_left: **{ev.get('player','?')}** a quitte le serveur."
    if t == "chat":
        return f"**{ev.get('player','?')}** {ev.get('text','')}"
    if t == "death":
        return f":skull_crossbones: {ev.get('text','')}"
    return None


# ── Flush queue → salon Discord ───────────────────────────────────────────────
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
#  MESSAGES DISCORD → MINECRAFT
#  Tout message qui ne commence pas par ! est envoye in-game
# ════════════════════════════════════════════════════════════════════════════
@bot.event
async def on_message(message: discord.Message):
    # Ignorer les messages du bot lui-meme
    if message.author.bot:
        return

    # Seulement dans le bon salon
    if message.channel.id != CHANNEL:
        await bot.process_commands(message)
        return

    content = message.content.strip()

    # Si c'est une commande (commence par !), traiter normalement
    if content.startswith("!"):
        await bot.process_commands(message)
        return

    # Sinon : envoyer le message in-game avec le pseudo Discord
    if content:
        author_name = message.author.display_name
        # Nettoyer le pseudo : enlever caractères speciaux Minecraft
        author_clean = re.sub(r'[§&]', '', author_name)
        # Limiter la longueur
        if len(content) > 200:
            content = content[:200] + "..."
        mc_cmd(f"say [Discord] {author_clean}: {content}")

    # NE PAS appeler process_commands ici (message normal, pas une commande)


# ════════════════════════════════════════════════════════════════════════════
#  COMMANDES
# ════════════════════════════════════════════════════════════════════════════

@bot.command(name="players", aliases=["pl", "list", "who"])
async def cmd_players(ctx):
    """!players — Joueurs connectes"""
    players = get_online_players()
    if not players:
        await ctx.send(":red_circle: Aucun joueur connecte.")
    else:
        names = ", ".join(f"**{p}**" for p in players)
        await ctx.send(
            f":green_circle: **{len(players)} joueur(s) en ligne:** {names}"
        )


@bot.command(name="status")
async def cmd_status(ctx):
    """!status — Statut complet"""
    stats   = get_stats()
    players = get_online_players()
    if not stats:
        await ctx.send(":red_circle: **Serveur hors ligne ou stats non disponibles.**")
        return
    tps_raw = stats.get("TPS", "N/A")
    try:
        tps_f   = float(tps_raw)
        tps_em  = ":green_circle:" if tps_f >= 18 else ":yellow_circle:" if tps_f >= 15 else ":red_circle:"
    except ValueError:
        tps_em  = ":white_circle:"
    lines = [
        ":satellite: **Statut du serveur**",
        "```",
        f"Heure  : {stats.get('TS',   'N/A')}",
        f"CPU    : {stats.get('CPU',  'N/A')}",
        f"RAM    : {stats.get('RAM',  'N/A')}",
        f"Swap   : {stats.get('SWAP', 'N/A')}",
        f"Disque : {stats.get('DISK', 'N/A')}",
        f"TPS    : {tps_raw}  {tps_em}",
        f"Online : {len(players)} joueur(s)",
        "```",
    ]
    if players:
        lines.append(
            f":busts_in_silhouette: {', '.join(f'**{p}**' for p in players)}"
        )
    await ctx.send("\n".join(lines))


@bot.command(name="tps")
async def cmd_tps(ctx):
    """!tps — TPS actuel"""
    stats   = get_stats()
    tps_raw = stats.get("TPS", "N/A")
    try:
        tps_f  = float(tps_raw)
        emoji  = ":green_circle:" if tps_f >= 18 else ":yellow_circle:" if tps_f >= 15 else ":red_circle:"
    except ValueError:
        emoji  = ":white_circle:"
    online = len(get_online_players())
    await ctx.send(
        f"{emoji} **TPS:** `{tps_raw}` | **Online:** `{online}` joueur(s)"
    )


@bot.command(name="kick")
async def cmd_kick(ctx, player: str = None, *, reason: str = "Kicked by admin"):
    """!kick <pseudo> [raison]"""
    if player is None:
        await ctx.send(":x: Usage: `!kick <pseudo> [raison]`")
        return
    mc_cmd(f"kick {player} {reason}")
    await ctx.send(f":boot: **{player}** a ete kick. Raison: `{reason}`")


@bot.command(name="ban")
async def cmd_ban(ctx, player: str = None, *, reason: str = "Banned by admin"):
    """!ban <pseudo> [raison]"""
    if player is None:
        await ctx.send(":x: Usage: `!ban <pseudo> [raison]`")
        return
    mc_cmd(f"ban {player} {reason}")
    await ctx.send(f":hammer: **{player}** a ete banni. Raison: `{reason}`")


@bot.command(name="unban", aliases=["pardon"])
async def cmd_unban(ctx, player: str = None):
    """!unban <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!unban <pseudo>`")
        return
    mc_cmd(f"pardon {player}")
    await ctx.send(f":white_check_mark: **{player}** a ete debanni.")


@bot.command(name="op")
async def cmd_op(ctx, player: str = None):
    """!op <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!op <pseudo>`")
        return
    mc_cmd(f"op {player}")
    await ctx.send(f":crown: **{player}** est maintenant OP.")


@bot.command(name="deop")
async def cmd_deop(ctx, player: str = None):
    """!deop <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!deop <pseudo>`")
        return
    mc_cmd(f"deop {player}")
    await ctx.send(f":no_entry: **{player}** n'est plus OP.")


@bot.command(name="say")
async def cmd_say(ctx, *, message: str = None):
    """!say <message> — Envoie dans le chat MC"""
    if message is None:
        await ctx.send(":x: Usage: `!say <message>`")
        return
    mc_cmd(f"say [Discord] {message}")
    await ctx.send(f":speech_balloon: Message envoye: `{message}`")


@bot.command(name="tp")
async def cmd_tp(ctx, player: str = None, target: str = None):
    """!tp <joueur> <destination>"""
    if player is None or target is None:
        await ctx.send(":x: Usage: `!tp <joueur> <destination>`")
        return
    mc_cmd(f"tp {player} {target}")
    await ctx.send(f":zap: **{player}** teleporte vers **{target}**.")


@bot.command(name="give")
async def cmd_give(ctx, player: str = None, item: str = None, amount: str = "1"):
    """!give <joueur> <item> [quantite]"""
    if player is None or item is None:
        await ctx.send(":x: Usage: `!give <joueur> <item> [quantite]`")
        return
    mc_cmd(f"give {player} {item} {amount}")
    await ctx.send(f":gift: Donne `{amount}x {item}` a **{player}**.")


@bot.command(name="gamemode", aliases=["gm"])
async def cmd_gamemode(ctx, player: str = None, mode: str = None):
    """!gamemode <joueur> <creative|survival|spectator|adventure>"""
    modes = {
        "c": "creative", "s": "survival", "sp": "spectator", "a": "adventure",
        "creative": "creative", "survival": "survival",
        "spectator": "spectator", "adventure": "adventure",
        "0": "survival", "1": "creative", "2": "adventure", "3": "spectator"
    }
    if player is None or mode is None:
        await ctx.send(":x: Usage: `!gamemode <joueur> <creative|survival|spectator|adventure>`")
        return
    resolved = modes.get(mode.lower(), mode)
    mc_cmd(f"gamemode {resolved} {player}")
    await ctx.send(f":joystick: Gamemode de **{player}** -> `{resolved}`.")


@bot.command(name="time")
async def cmd_time(ctx, value: str = None):
    """!time <day|night|noon|midnight|0-24000>"""
    if value is None:
        await ctx.send(":x: Usage: `!time <day|night|noon|midnight>`")
        return
    mc_cmd(f"time set {value}")
    await ctx.send(f":clock3: Heure -> `{value}`.")


@bot.command(name="weather")
async def cmd_weather(ctx, value: str = None):
    """!weather <clear|rain|thunder>"""
    if value is None:
        await ctx.send(":x: Usage: `!weather <clear|rain|thunder>`")
        return
    mc_cmd(f"weather {value}")
    await ctx.send(f":cloud: Meteo -> `{value}`.")


@bot.command(name="difficulty")
async def cmd_difficulty(ctx, value: str = None):
    """!difficulty <peaceful|easy|normal|hard>"""
    if value is None:
        await ctx.send(":x: Usage: `!difficulty <peaceful|easy|normal|hard>`")
        return
    mc_cmd(f"difficulty {value}")
    await ctx.send(f":crossed_swords: Difficulte -> `{value}`.")


@bot.command(name="whitelist", aliases=["wl"])
async def cmd_whitelist(ctx, action: str = None, player: str = None):
    """!whitelist <add|remove|on|off|list> [joueur]"""
    if action is None:
        await ctx.send(":x: Usage: `!whitelist <add|remove|on|off|list> [joueur]`")
        return
    if action in ("add", "remove"):
        if player is None:
            await ctx.send(f":x: Usage: `!whitelist {action} <joueur>`")
            return
        mc_cmd(f"whitelist {action} {player}")
        await ctx.send(f":notepad_spiral: Whitelist `{action}` -> **{player}**.")
    elif action in ("on", "off", "list"):
        mc_cmd(f"whitelist {action}")
        await ctx.send(f":notepad_spiral: Whitelist -> `{action}`.")
    else:
        await ctx.send(":x: Action invalide. Utilise: add, remove, on, off, list")


@bot.command(name="heal")
async def cmd_heal(ctx, player: str = None):
    """!heal <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!heal <pseudo>`")
        return
    mc_cmd(f"heal {player}")
    await ctx.send(f":heart: **{player}** soigne.")


@bot.command(name="fly")
async def cmd_fly(ctx, player: str = None):
    """!fly <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!fly <pseudo>`")
        return
    mc_cmd(f"fly {player}")
    await ctx.send(f":airplane: Vol toggle pour **{player}**.")


@bot.command(name="clear")
async def cmd_clear(ctx, player: str = None):
    """!clear <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!clear <pseudo>`")
        return
    mc_cmd(f"clear {player}")
    await ctx.send(f":wastebasket: Inventaire de **{player}** vide.")


@bot.command(name="broadcast", aliases=["bc"])
async def cmd_broadcast(ctx, *, message: str = None):
    """!broadcast <message>"""
    if message is None:
        await ctx.send(":x: Usage: `!broadcast <message>`")
        return
    mc_cmd(f"broadcast {message}")
    await ctx.send(f":mega: Broadcast: `{message}`")


@bot.command(name="sudo")
async def cmd_sudo(ctx, *, command: str = None):
    """!sudo <commande brute MC>"""
    if command is None:
        await ctx.send(":x: Usage: `!sudo <commande>`")
        return
    mc_cmd(command)
    await ctx.send(f":computer: Commande envoyee: `{command}`")


@bot.command(name="stop")
async def cmd_stop(ctx):
    """!stop — Arrete le serveur"""
    mc_cmd("stop")
    await ctx.send(":octagonal_sign: Arret du serveur envoye.")


@bot.command(name="restart")
async def cmd_restart(ctx):
    """!restart — Relance le workflow"""
    url     = f"https://api.github.com/repos/{REPO}/actions/workflows/server.yml/dispatches"
    headers = {
        "Authorization": f"token {PAT}",
        "Accept":        "application/vnd.github.v3+json"
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, json={"ref": "main"}) as r:
            if r.status == 204:
                await ctx.send(":white_check_mark: Serveur relance !")
            else:
                await ctx.send(f":x: Erreur {r.status}")


@bot.command(name="logs")
async def cmd_logs(ctx, nb: int = 20):
    """!logs [nb] — Derniers logs MC"""
    if not os.path.isfile(MC_LOG):
        await ctx.send(f":x: Log introuvable: `{MC_LOG}`")
        return
    nb = min(max(nb, 1), 50)
    try:
        result = subprocess.run(
            ["tail", f"-{nb}", MC_LOG],
            capture_output=True, text=True, timeout=5
        )
        content = result.stdout.strip()
        if not content:
            await ctx.send(":x: Log vide.")
            return
        # Tronquer si trop long
        if len(content) > 1900:
            content = "..." + content[-1897:]
        await ctx.send(f"```\n{content}\n```")
    except Exception as e:
        await ctx.send(f":x: Erreur: `{e}`")


@bot.command(name="help_mc", aliases=["cmds", "commands", "aide"])
async def cmd_help(ctx):
    """!help_mc — Liste des commandes"""
    help_text = (
        ":joystick: **Commandes Bot Minecraft**\n\n"
        "**Info**\n"
        "`!players` `!pl` `!who` — Joueurs en ligne\n"
        "`!status` — CPU / RAM / TPS / joueurs\n"
        "`!tps` — TPS actuel\n"
        "`!logs [nb]` — Derniers logs\n\n"
        "**Moderation**\n"
        "`!kick <pseudo> [raison]` — Kick\n"
        "`!ban <pseudo> [raison]` — Ban\n"
        "`!unban <pseudo>` — Unban\n"
        "`!op <pseudo>` — Donner OP\n"
        "`!deop <pseudo>` — Retirer OP\n"
        "`!whitelist <add|remove|on|off|list> [pseudo]`\n\n"
        "**Joueur**\n"
        "`!tp <joueur> <dest>` — Teleporter\n"
        "`!gamemode <joueur> <mode>` — Gamemode\n"
        "`!give <joueur> <item> [qte]` — Donner item\n"
        "`!heal <pseudo>` — Soigner\n"
        "`!fly <pseudo>` — Toggle vol\n"
        "`!clear <pseudo>` — Vider inventaire\n\n"
        "**Monde**\n"
        "`!time <day|night|noon|midnight>` — Heure\n"
        "`!weather <clear|rain|thunder>` — Meteo\n"
        "`!difficulty <peaceful|easy|normal|hard>` — Difficulte\n\n"
        "**Serveur**\n"
        "`!say <message>` — Chat MC\n"
        "`!broadcast <message>` — Broadcast\n"
        "`!restart` — Relancer\n"
        "`!stop` — Arreter\n"
        "`!sudo <commande>` — Commande brute\n\n"
        ":speech_balloon: **Ecrire normalement** dans ce salon envoie le message in-game !"
    )
    await ctx.send(help_text)


# ── Events bot ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[Bot] Connecte: {bot.user}", flush=True)
    flush_queue.start()
    asyncio.create_task(event_reader())


bot.run(TOKEN)
