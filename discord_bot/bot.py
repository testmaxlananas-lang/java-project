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

# ── Joueurs en ligne (suivi par events) ──────────────────────────────────────
_online_players: set[str] = set()

def get_online_players() -> list[str]:
    return sorted(_online_players)


# ── Envoi commande MC ─────────────────────────────────────────────────────────
def mc_cmd(cmd: str) -> bool:
    try:
        with open(MC_PIPE, "w") as f:
            f.write(cmd + "\n")
            f.flush()
        return True
    except Exception as e:
        print(f"[Bot] mc_cmd erreur: {e}", flush=True)
        return False


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


# ── Verifier si le workflow est actif ────────────────────────────────────────
async def is_workflow_running() -> bool:
    url = (
        f"https://api.github.com/repos/{REPO}"
        f"/actions/workflows/server.yml/runs"
        f"?status=in_progress&per_page=1"
    )
    headers = {
        "Authorization": f"token {PAT}",
        "Accept":        "application/vnd.github.v3+json"
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return False
                data = await r.json()
                runs = data.get("workflow_runs", [])
                return len(runs) > 0
    except Exception as e:
        print(f"[Bot] is_workflow_running erreur: {e}", flush=True)
        return False


# ── Lancer le workflow ────────────────────────────────────────────────────────
async def dispatch_workflow() -> bool:
    url = (
        f"https://api.github.com/repos/{REPO}"
        f"/actions/workflows/server.yml/dispatches"
    )
    headers = {
        "Authorization": f"token {PAT}",
        "Accept":        "application/vnd.github.v3+json"
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                url, headers=headers,
                json={"ref": "main"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                return r.status == 204
    except Exception as e:
        print(f"[Bot] dispatch_workflow erreur: {e}", flush=True)
        return False


# ── Lecture events MC ─────────────────────────────────────────────────────────
async def event_reader():
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
        return (
            f":white_check_mark: **Le serveur est en ligne !**"
            f" JVM: `{jvm}` | CPUs: `{cpu}`"
        )
    if t == "stop":
        return ":octagonal_sign: **Le serveur est arrete.**"
    if t == "restart":
        return (
            f":arrows_counterclockwise: **Redemarrage automatique**"
            f" apres `{ev.get('minutes','?')}` minutes."
        )
    if t == "crash":
        return (
            f":red_circle: **Crash #{ev.get('count','?')}**"
            f" — `{ev.get('cause','?')}` — Redemarrage dans 10s..."
        )
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


# ── Flush queue → Discord ─────────────────────────────────────────────────────
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
# ════════════════════════════════════════════════════════════════════════════
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.channel.id != CHANNEL:
        await bot.process_commands(message)
        return

    content = message.content.strip()

    if content.startswith("!"):
        await bot.process_commands(message)
        return

    if content:
        author_clean = re.sub(r"[§&]", "", message.author.display_name)
        text         = content[:200] + "..." if len(content) > 200 else content
        mc_cmd(f"say [Discord] {author_clean}: {text}")


# ════════════════════════════════════════════════════════════════════════════
#  COMMANDES
# ════════════════════════════════════════════════════════════════════════════

# ── !start ────────────────────────────────────────────────────────────────────
@bot.command(name="start")
async def cmd_start(ctx):
    """!start — Lance le serveur s'il est arrete"""
    msg = await ctx.send(":hourglass: Verification du statut du serveur...")

    running = await is_workflow_running()

    if running:
        await msg.edit(
            content=":green_circle: Le serveur est **deja en ligne** !"
        )
        return

    await msg.edit(content=":rocket: Serveur arrete, lancement en cours...")
    ok = await dispatch_workflow()

    if ok:
        await msg.edit(
            content=(
                ":white_check_mark: **Workflow lance !**\n"
                ":clock3: Le serveur sera en ligne dans ~2-3 minutes."
            )
        )
    else:
        await msg.edit(
            content=(
                ":x: **Echec du lancement.**\n"
                "Verifie que le PAT a les permissions `Actions: write`."
            )
        )


# ── !restart ──────────────────────────────────────────────────────────────────
@bot.command(name="restart")
async def cmd_restart(ctx):
    """!restart — Relance le workflow (force meme si deja actif)"""
    msg = await ctx.send(":hourglass: Lancement du redemarrage...")
    ok  = await dispatch_workflow()
    if ok:
        await msg.edit(
            content=(
                ":arrows_counterclockwise: **Redemarrage lance !**\n"
                ":clock3: Le serveur sera de retour dans ~2-3 minutes."
            )
        )
    else:
        await msg.edit(content=":x: Echec du dispatch. Verifie le PAT.")


# ── !stop ─────────────────────────────────────────────────────────────────────
@bot.command(name="stop")
async def cmd_stop(ctx):
    """!stop — Arrete le serveur Minecraft"""
    mc_cmd("stop")
    await ctx.send(":octagonal_sign: Commande d'arret envoyee au serveur.")


# ── !players ──────────────────────────────────────────────────────────────────
@bot.command(name="players", aliases=["pl", "list", "who"])
async def cmd_players(ctx):
    """!players — Joueurs connectes"""
    players = get_online_players()
    if not players:
        await ctx.send(":red_circle: Aucun joueur connecte.")
    else:
        names = ", ".join(f"**{p}**" for p in players)
        await ctx.send(
            f":green_circle: **{len(players)} joueur(s) en ligne :** {names}"
        )


# ── !status ───────────────────────────────────────────────────────────────────
@bot.command(name="status")
async def cmd_status(ctx):
    """!status — Statut complet du serveur"""
    msg = await ctx.send(":hourglass: Recuperation du statut...")

    running = await is_workflow_running()
    stats   = get_stats()
    players = get_online_players()

    if not running:
        await msg.edit(content=":red_circle: **Le serveur est hors ligne.**")
        return

    if not stats:
        await msg.edit(
            content=":yellow_circle: **Serveur en demarrage...** (stats pas encore disponibles)"
        )
        return

    tps_raw = stats.get("TPS", "N/A")
    try:
        tps_f  = float(tps_raw)
        tps_em = (
            ":green_circle:"  if tps_f >= 18 else
            ":yellow_circle:" if tps_f >= 15 else
            ":red_circle:"
        )
    except ValueError:
        tps_em = ":white_circle:"

    lines = [
        ":satellite: **Statut du serveur**",
        "```",
        f"Heure  : {stats.get('TS',   'N/A')}",
        f"CPU    : {stats.get('CPU',  'N/A')}",
        f"RAM    : {stats.get('RAM',  'N/A')}",
        f"Swap   : {stats.get('SWAP', 'N/A')}",
        f"Disque : {stats.get('DISK', 'N/A')}",
        f"TPS    : {tps_raw}",
        f"Online : {len(players)} joueur(s)",
        "```",
        f"{tps_em} TPS",
    ]
    if players:
        lines.append(
            f":busts_in_silhouette: {', '.join(f'**{p}**' for p in players)}"
        )

    await msg.edit(content="\n".join(lines))


# ── !tps ──────────────────────────────────────────────────────────────────────
@bot.command(name="tps")
async def cmd_tps(ctx):
    """!tps — TPS actuel"""
    stats   = get_stats()
    tps_raw = stats.get("TPS", "N/A")
    try:
        tps_f  = float(tps_raw)
        emoji  = (
            ":green_circle:"  if tps_f >= 18 else
            ":yellow_circle:" if tps_f >= 15 else
            ":red_circle:"
        )
    except ValueError:
        emoji = ":white_circle:"
    online = len(get_online_players())
    await ctx.send(
        f"{emoji} **TPS:** `{tps_raw}` | **Online:** `{online}` joueur(s)"
    )


# ── !kick ─────────────────────────────────────────────────────────────────────
@bot.command(name="kick")
async def cmd_kick(ctx, player: str = None, *, reason: str = "Kicked by admin"):
    """!kick <pseudo> [raison]"""
    if player is None:
        await ctx.send(":x: Usage: `!kick <pseudo> [raison]`")
        return
    mc_cmd(f"kick {player} {reason}")
    await ctx.send(f":boot: **{player}** a ete kick. Raison: `{reason}`")


# ── !ban ──────────────────────────────────────────────────────────────────────
@bot.command(name="ban")
async def cmd_ban(ctx, player: str = None, *, reason: str = "Banned by admin"):
    """!ban <pseudo> [raison]"""
    if player is None:
        await ctx.send(":x: Usage: `!ban <pseudo> [raison]`")
        return
    mc_cmd(f"ban {player} {reason}")
    await ctx.send(f":hammer: **{player}** a ete banni. Raison: `{reason}`")


# ── !unban ────────────────────────────────────────────────────────────────────
@bot.command(name="unban", aliases=["pardon"])
async def cmd_unban(ctx, player: str = None):
    """!unban <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!unban <pseudo>`")
        return
    mc_cmd(f"pardon {player}")
    await ctx.send(f":white_check_mark: **{player}** a ete debanni.")


# ── !op / !deop ───────────────────────────────────────────────────────────────
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


# ── !say ──────────────────────────────────────────────────────────────────────
@bot.command(name="say")
async def cmd_say(ctx, *, message: str = None):
    """!say <message>"""
    if message is None:
        await ctx.send(":x: Usage: `!say <message>`")
        return
    mc_cmd(f"say [Discord] {message}")
    await ctx.send(f":speech_balloon: Message envoye: `{message}`")


# ── !broadcast ────────────────────────────────────────────────────────────────
@bot.command(name="broadcast", aliases=["bc"])
async def cmd_broadcast(ctx, *, message: str = None):
    """!broadcast <message>"""
    if message is None:
        await ctx.send(":x: Usage: `!broadcast <message>`")
        return
    mc_cmd(f"broadcast {message}")
    await ctx.send(f":mega: Broadcast: `{message}`")


# ── !tp ───────────────────────────────────────────────────────────────────────
@bot.command(name="tp")
async def cmd_tp(ctx, player: str = None, target: str = None):
    """!tp <joueur> <destination>"""
    if player is None or target is None:
        await ctx.send(":x: Usage: `!tp <joueur> <destination>`")
        return
    mc_cmd(f"tp {player} {target}")
    await ctx.send(f":zap: **{player}** teleporte vers **{target}**.")


# ── !give ─────────────────────────────────────────────────────────────────────
@bot.command(name="give")
async def cmd_give(ctx, player: str = None, item: str = None, amount: str = "1"):
    """!give <joueur> <item> [quantite]"""
    if player is None or item is None:
        await ctx.send(":x: Usage: `!give <joueur> <item> [quantite]`")
        return
    mc_cmd(f"give {player} {item} {amount}")
    await ctx.send(f":gift: Donne `{amount}x {item}` a **{player}**.")


# ── !gamemode ─────────────────────────────────────────────────────────────────
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
        await ctx.send(
            ":x: Usage: `!gamemode <joueur> <creative|survival|spectator|adventure>`"
        )
        return
    resolved = modes.get(mode.lower(), mode)
    mc_cmd(f"gamemode {resolved} {player}")
    await ctx.send(f":joystick: Gamemode de **{player}** -> `{resolved}`.")


# ── !time ─────────────────────────────────────────────────────────────────────
@bot.command(name="time")
async def cmd_time(ctx, value: str = None):
    """!time <day|night|noon|midnight|0-24000>"""
    if value is None:
        await ctx.send(":x: Usage: `!time <day|night|noon|midnight>`")
        return
    mc_cmd(f"time set {value}")
    await ctx.send(f":clock3: Heure -> `{value}`.")


# ── !weather ──────────────────────────────────────────────────────────────────
@bot.command(name="weather")
async def cmd_weather(ctx, value: str = None):
    """!weather <clear|rain|thunder>"""
    if value is None:
        await ctx.send(":x: Usage: `!weather <clear|rain|thunder>`")
        return
    mc_cmd(f"weather {value}")
    await ctx.send(f":cloud: Meteo -> `{value}`.")


# ── !difficulty ───────────────────────────────────────────────────────────────
@bot.command(name="difficulty")
async def cmd_difficulty(ctx, value: str = None):
    """!difficulty <peaceful|easy|normal|hard>"""
    if value is None:
        await ctx.send(":x: Usage: `!difficulty <peaceful|easy|normal|hard>`")
        return
    mc_cmd(f"difficulty {value}")
    await ctx.send(f":crossed_swords: Difficulte -> `{value}`.")


# ── !whitelist ────────────────────────────────────────────────────────────────
@bot.command(name="whitelist", aliases=["wl"])
async def cmd_whitelist(ctx, action: str = None, player: str = None):
    """!whitelist <add|remove|on|off|list> [joueur]"""
    if action is None:
        await ctx.send(
            ":x: Usage: `!whitelist <add|remove|on|off|list> [joueur]`"
        )
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
        await ctx.send(":x: Actions valides: `add`, `remove`, `on`, `off`, `list`")


# ── !heal ─────────────────────────────────────────────────────────────────────
@bot.command(name="heal")
async def cmd_heal(ctx, player: str = None):
    """!heal <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!heal <pseudo>`")
        return
    mc_cmd(f"heal {player}")
    await ctx.send(f":heart: **{player}** soigne.")


# ── !fly ──────────────────────────────────────────────────────────────────────
@bot.command(name="fly")
async def cmd_fly(ctx, player: str = None):
    """!fly <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!fly <pseudo>`")
        return
    mc_cmd(f"fly {player}")
    await ctx.send(f":airplane: Vol toggle pour **{player}**.")


# ── !clear ────────────────────────────────────────────────────────────────────
@bot.command(name="clear")
async def cmd_clear(ctx, player: str = None):
    """!clear <pseudo>"""
    if player is None:
        await ctx.send(":x: Usage: `!clear <pseudo>`")
        return
    mc_cmd(f"clear {player}")
    await ctx.send(f":wastebasket: Inventaire de **{player}** vide.")


# ── !sudo ─────────────────────────────────────────────────────────────────────
@bot.command(name="sudo")
async def cmd_sudo(ctx, *, command: str = None):
    """!sudo <commande brute MC>"""
    if command is None:
        await ctx.send(":x: Usage: `!sudo <commande>`")
        return
    mc_cmd(command)
    await ctx.send(f":computer: Commande envoyee: `{command}`")


# ── !logs ─────────────────────────────────────────────────────────────────────
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
        if len(content) > 1900:
            content = "..." + content[-1897:]
        await ctx.send(f"```\n{content}\n```")
    except Exception as e:
        await ctx.send(f":x: Erreur: `{e}`")


# ── !help_mc ──────────────────────────────────────────────────────────────────
@bot.command(name="help_mc", aliases=["cmds", "commands", "aide"])
async def cmd_help(ctx):
    """!help_mc — Liste des commandes"""
    help_text = (
        ":joystick: **Commandes Bot Minecraft**\n\n"
        "**Serveur**\n"
        "`!start` — Lancer le serveur s'il est arrete\n"
        "`!restart` — Relancer le workflow\n"
        "`!stop` — Arreter le serveur\n"
        "`!status` — Statut complet\n"
        "`!tps` — TPS actuel\n"
        "`!logs [nb]` — Derniers logs\n\n"
        "**Joueurs**\n"
        "`!players` `!pl` `!who` — Joueurs en ligne\n"
        "`!kick <pseudo> [raison]` — Kick\n"
        "`!ban <pseudo> [raison]` — Ban\n"
        "`!unban <pseudo>` — Unban\n"
        "`!op <pseudo>` — Donner OP\n"
        "`!deop <pseudo>` — Retirer OP\n"
        "`!whitelist <add|remove|on|off|list> [pseudo]`\n\n"
        "**Actions joueur**\n"
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
        "**Chat**\n"
        "`!say <message>` — Envoyer dans le chat MC\n"
        "`!broadcast <message>` — Broadcast\n"
        "`!sudo <commande>` — Commande brute\n\n"
        ":speech_balloon: **Ecrire normalement** dans ce salon envoie le message in-game !"
    )
    await ctx.send(help_text)


# ── on_ready ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[Bot] Connecte: {bot.user}", flush=True)
    flush_queue.start()
    asyncio.create_task(event_reader())


bot.run(TOKEN)
