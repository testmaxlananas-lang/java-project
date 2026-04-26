import discord
import aiohttp
import asyncio
import os
import re
import json
import subprocess
from discord.ext import commands, tasks

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN      = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID   = int(os.environ["DISCORD_GUILD_ID"])
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
PAT        = os.environ["GITHUB_PAT"]
REPO       = os.environ["GITHUB_REPO"]
EVENT_FILE = os.environ.get("MC_EVENT_FILE",  "/tmp/mc_events.jsonl")
MC_PIPE    = os.environ.get("MC_PIPE",        "/tmp/mc_input")
STATS_FILE = os.environ.get("MC_STATS_FILE",  "/tmp/mc_stats.txt")
MC_LOG     = os.environ.get("MC_LOG",
    "/home/runner/work/java-project/java-project/mc.log")

# ── Rôles Discord → préfixe Minecraft ────────────────────────────────────────
# Format : ID_ROLE → (emoji_prefix, couleur_hex_mm, priorité)
ROLE_MAP: dict[int, tuple[str, str, int]] = {
    13138440: ("☩ Grandmaster", "#C87A08", 100),
    8947848:  ("🌑 Mantled",    "#888888",  90),
    13936723: ("🕯 Lightkeeper","#D4A853",  80),
    8026746:  ("🪨 Stonecutter","#7A7A7A",  70),
    6000056:  ("⚙ Artificer",  "#5B8DB8",  60),
    10187471: ("🖋 Illuminator","#9B72CF",  50),
    10514767: ("🗿 Sculptor",   "#A0714F",  40),
    7035454:  ("🕯 Initiate",  "#6B5A3E",  30),
    3050327:  ("🤝 Ally",      "#2E8B57",  20),
    4881497:  ("🌿 Wanderer",  "#4A7C59",  15),
    5592405:  ("🚪 Outsider",  "#555555",  10),
    # Donateurs
    46296:    ("💎 Patron",    "#00B4D8", 110),
    9133302:  ("🔮 Benefactor","#8B5CF6", 120),
    13215820: ("👑 Magnate",   "#C9A84C", 130),
    10519644: ("🪙 Contractor","#A0845C",  85),
    2899536:  ("⚙ Construct",  "#2C3E50",   5),
}

# ── Intents ───────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True   # Pour lire les rôles des membres
bot = commands.Bot(command_prefix="!", intents=intents)

msg_queue: asyncio.Queue = asyncio.Queue()
_online_players: set[str] = set()


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITAIRES RÔLES
# ═══════════════════════════════════════════════════════════════════════════════

def get_member_role(member: discord.Member) -> tuple[str, str] | None:
    """
    Retourne (emoji_nom, couleur_hex) du rôle le plus prioritaire du membre.
    Retourne None si aucun rôle WM.
    """
    best     = None
    best_pri = -1
    for role in member.roles:
        data = ROLE_MAP.get(role.id)
        if data and data[2] > best_pri:
            best     = data
            best_pri = data[2]
    if best is None:
        return None
    return (best[0], best[1])


def format_role_prefix(member: discord.Member) -> str:
    """
    Retourne le préfixe coloré pour le chat MC.
    Ex: §x§C§8§7§A§0§8☩ Grandmaster§r
    """
    role = get_member_role(member)
    if role is None:
        return ""
    name, hexcol = role
    # Convertir #RRGGBB → §x§R§R§G§G§B§B
    col = hexcol.lstrip("#")
    r, g, b = col[0:2], col[2:4], col[4:6]
    mc_color = f"§x§{r[0]}§{r[1]}§{g[0]}§{g[1]}§{b[0]}§{b[1]}"
    return f"{mc_color}{name}§r "


def format_role_tag(member: discord.Member) -> str:
    """
    Retourne une balise MiniMessage pour le nametag.
    Ex: <color:#C87A08>☩ Grandmaster</color>
    """
    role = get_member_role(member)
    if role is None:
        return ""
    name, hexcol = role
    return f"<color:{hexcol}>{name}</color> "


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITAIRES MC / STATS
# ═══════════════════════════════════════════════════════════════════════════════

def mc_cmd(cmd: str) -> bool:
    try:
        with open(MC_PIPE, "w") as f:
            f.write(cmd + "\n")
            f.flush()
        return True
    except Exception as e:
        print(f"[Bot] mc_cmd erreur: {e}", flush=True)
        return False


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


def get_online_players() -> list[str]:
    return sorted(_online_players)


# ═══════════════════════════════════════════════════════════════════════════════
# GITHUB API
# ═══════════════════════════════════════════════════════════════════════════════

async def is_workflow_running() -> bool:
    url = (
        f"https://api.github.com/repos/{REPO}"
        "/actions/workflows/server.yml/runs"
        "?status=in_progress&per_page=1"
    )
    headers = {
        "Authorization": f"token {PAT}",
        "Accept": "application/vnd.github.v3+json"
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    return False
                data = await r.json()
                return len(data.get("workflow_runs", [])) > 0
    except Exception as e:
        print(f"[Bot] is_workflow_running erreur: {e}", flush=True)
        return False


async def dispatch_workflow() -> bool:
    url = (
        f"https://api.github.com/repos/{REPO}"
        "/actions/workflows/server.yml/dispatches"
    )
    headers = {
        "Authorization": f"token {PAT}",
        "Accept": "application/vnd.github.v3+json"
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


# ═══════════════════════════════════════════════════════════════════════════════
# LECTEUR D'ÉVÉNEMENTS MC
# ═══════════════════════════════════════════════════════════════════════════════

async def event_reader():
    waited = 0
    while not os.path.isfile(EVENT_FILE):
        await asyncio.sleep(1)
        waited += 1
        if waited > 120:
            print("[Bot] Timeout EVENT_FILE", flush=True)
            return

    print(f"[Bot] Lecture events: {EVENT_FILE}", flush=True)
    loop = asyncio.get_event_loop()

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

    pos = os.path.getsize(EVENT_FILE) \
        if os.path.isfile(EVENT_FILE) else 0

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
        lines, pos = await loop.run_in_executor(
            None, read_lines, pos)
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
    match t:
        case "start":
            jvm = ev.get("jvm", "?")
            cpu = ev.get("cpus", "?")
            return (
                "✅ **Serveur en ligne !**"
                f" `JVM: {jvm}` | `CPUs: {cpu}`"
            )
        case "stop":
            return "🛑 **Serveur arrêté.**"
        case "restart":
            return (
                "🔄 **Redémarrage automatique** après"
                f" `{ev.get('minutes','?')}` min."
            )
        case "crash":
            return (
                f"🔴 **Crash #{ev.get('count','?')}**"
                f" — `{ev.get('cause','?')}`"
                " — Redémarrage dans 10s..."
            )
        case "crash_fatal":
            return "💀 **Crash fatal** — Arrêt définitif."
        case "join":
            return (
                f"➡️ **{ev.get('player','?')}**"
                " a rejoint le serveur."
            )
        case "quit":
            return (
                f"⬅️ **{ev.get('player','?')}**"
                " a quitté le serveur."
            )
        case "chat":
            return (
                f"💬 **{ev.get('player','?')}** :"
                f" {ev.get('text','')}"
            )
        case "death":
            return f"💀 {ev.get('text','')}"
        case _:
            return None


# ═══════════════════════════════════════════════════════════════════════════════
# FLUSH QUEUE → DISCORD
# ═══════════════════════════════════════════════════════════════════════════════

@tasks.loop(seconds=0.5)
async def flush_queue():
    channel = bot.get_channel(CHANNEL_ID)
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


# ═══════════════════════════════════════════════════════════════════════════════
# CHAT DISCORD → MC (avec rôle)
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_ID:
        await bot.process_commands(message)
        return

    content = message.content.strip()
    if content.startswith("!"):
        await bot.process_commands(message)
        return

    if content:
        # Rôle du membre
        role_prefix = ""
        if isinstance(message.author, discord.Member):
            role_prefix = format_role_prefix(message.author)

        name  = re.sub(r"[§&]", "", message.author.display_name)
        text  = content[:180] + "..." if len(content) > 180 else content
        # Format MC : [rôle] [Discord] Pseudo: message
        mc_cmd(
            f"say {role_prefix}"
            f"§7[§bDiscord§7] §f{name}§7: §f{text}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# EMBED UTILITAIRES
# ═══════════════════════════════════════════════════════════════════════════════

def make_embed(
    title: str,
    description: str = "",
    color: int = 0x00B4DB,
    fields: list[tuple[str, str, bool]] | None = None
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color
    )
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    return embed


# ═══════════════════════════════════════════════════════════════════════════════
# COMMANDES BOT
# ═══════════════════════════════════════════════════════════════════════════════

# ── !start ────────────────────────────────────────────────────────────────────
@bot.command(name="start")
async def cmd_start(ctx: commands.Context):
    """Lance le serveur s'il est arrêté."""
    msg = await ctx.send(embed=make_embed(
        "⏳ Vérification...", "Contrôle du statut du serveur."))

    if await is_workflow_running():
        await msg.edit(embed=make_embed(
            "🟢 Déjà en ligne",
            "Le serveur est **déjà en ligne** !",
            color=0x2ECC71))
        return

    await msg.edit(embed=make_embed(
        "🚀 Lancement...", "Démarrage du workflow GitHub Actions.",
        color=0xF39C12))
    ok = await dispatch_workflow()

    if ok:
        await msg.edit(embed=make_embed(
            "✅ Workflow lancé !",
            "Le serveur sera en ligne dans **~2-3 minutes**.",
            color=0x2ECC71))
    else:
        await msg.edit(embed=make_embed(
            "❌ Échec", "Vérifiez que le PAT a `Actions: write`.",
            color=0xE74C3C))


# ── !restart ──────────────────────────────────────────────────────────────────
@bot.command(name="restart")
async def cmd_restart(ctx: commands.Context):
    """Relance le workflow."""
    msg = await ctx.send(embed=make_embed(
        "🔄 Redémarrage...", "", color=0xF39C12))
    ok = await dispatch_workflow()
    if ok:
        await msg.edit(embed=make_embed(
            "✅ Redémarrage lancé !",
            "Le serveur sera de retour dans **~2-3 min**.",
            color=0x2ECC71))
    else:
        await msg.edit(embed=make_embed(
            "❌ Échec", "Vérifiez le PAT.", color=0xE74C3C))


# ── !stop ─────────────────────────────────────────────────────────────────────
@bot.command(name="stop")
async def cmd_stop(ctx: commands.Context):
    """Arrête le serveur Minecraft."""
    mc_cmd("stop")
    await ctx.send(embed=make_embed(
        "🛑 Arrêt envoyé",
        "Le serveur va s'arrêter proprement.",
        color=0xE74C3C))


# ── !status ───────────────────────────────────────────────────────────────────
@bot.command(name="status")
async def cmd_status(ctx: commands.Context):
    """Statut complet du serveur."""
    msg = await ctx.send(embed=make_embed(
        "⏳ Récupération...", "", color=0xF39C12))

    running = await is_workflow_running()
    stats   = get_stats()
    players = get_online_players()

    if not running:
        await msg.edit(embed=make_embed(
            "🔴 Serveur hors ligne",
            "Le serveur est **arrêté**.",
            color=0xE74C3C))
        return

    if not stats:
        await msg.edit(embed=make_embed(
            "🟡 Démarrage en cours...",
            "Stats pas encore disponibles.",
            color=0xF39C12))
        return

    tps_raw = stats.get("TPS", "N/A")
    try:
        tps_f = float(tps_raw)
        tps_color = (
            0x2ECC71 if tps_f >= 18 else
            0xF39C12 if tps_f >= 15 else
            0xE74C3C
        )
        tps_icon = (
            "🟢" if tps_f >= 18 else
            "🟡" if tps_f >= 15 else
            "🔴"
        )
    except ValueError:
        tps_f     = 0.0
        tps_color = 0x95A5A6
        tps_icon  = "⚪"

    fields = [
        ("🕐 Heure",   stats.get("TS",   "N/A"), True),
        ("💻 CPU",      stats.get("CPU",  "N/A"), True),
        ("🧠 RAM",      stats.get("RAM",  "N/A"), True),
        ("💾 Swap",     stats.get("SWAP", "N/A"), True),
        ("💿 Disque",   stats.get("DISK", "N/A"), True),
        (f"{tps_icon} TPS", tps_raw,              True),
    ]

    if players:
        fields.append((
            f"👥 Joueurs ({len(players)})",
            ", ".join(f"`{p}`" for p in players),
            False
        ))
    else:
        fields.append(("👥 Joueurs", "Aucun", True))

    await msg.edit(embed=make_embed(
        "🟢 Serveur en ligne",
        fields=fields,
        color=tps_color))


# ── !players ──────────────────────────────────────────────────────────────────
@bot.command(name="players", aliases=["pl", "list", "who"])
async def cmd_players(ctx: commands.Context):
    """Joueurs connectés."""
    players = get_online_players()
    if not players:
        await ctx.send(embed=make_embed(
            "👥 Joueurs en ligne",
            "🔴 Aucun joueur connecté.",
            color=0xE74C3C))
    else:
        names = "\n".join(f"• `{p}`" for p in players)
        await ctx.send(embed=make_embed(
            f"👥 {len(players)} joueur(s) en ligne",
            names, color=0x2ECC71))


# ── !tps ──────────────────────────────────────────────────────────────────────
@bot.command(name="tps")
async def cmd_tps(ctx: commands.Context):
    """TPS actuel."""
    stats   = get_stats()
    tps_raw = stats.get("TPS", "N/A")
    try:
        tps_f = float(tps_raw)
        icon  = "🟢" if tps_f >= 18 else "🟡" if tps_f >= 15 else "🔴"
        color = 0x2ECC71 if tps_f >= 18 else 0xF39C12 if tps_f >= 15 else 0xE74C3C
    except ValueError:
        icon  = "⚪"
        color = 0x95A5A6
    online = len(get_online_players())
    await ctx.send(embed=make_embed(
        f"{icon} TPS : {tps_raw}",
        f"**Online :** `{online}` joueur(s)",
        color=color))


# ── !role ─────────────────────────────────────────────────────────────────────
@bot.command(name="role")
async def cmd_role(ctx: commands.Context,
                   member: discord.Member | None = None):
    """!role [@membre] — Affiche le rôle WM d'un membre."""
    target = member or (
        ctx.author if isinstance(ctx.author, discord.Member)
        else None)
    if target is None:
        await ctx.send(embed=make_embed(
            "❌ Erreur", "Impossible de récupérer le membre.",
            color=0xE74C3C))
        return

    role = get_member_role(target)
    if role is None:
        await ctx.send(embed=make_embed(
            f"Rôle de {target.display_name}",
            "🚪 Aucun rôle WorldManager.",
            color=0x555555))
    else:
        name, hexcol = role
        col = int(hexcol.lstrip("#"), 16)
        await ctx.send(embed=make_embed(
            f"Rôle de {target.display_name}",
            f"{name}\n`Couleur: {hexcol}`",
            color=col))


# ── !roles ────────────────────────────────────────────────────────────────────
@bot.command(name="roles")
async def cmd_roles(ctx: commands.Context):
    """!roles — Liste tous les rôles disponibles."""
    lines = []
    sorted_roles = sorted(
        ROLE_MAP.values(),
        key=lambda x: x[2],
        reverse=True
    )
    for name, hexcol, pri in sorted_roles:
        lines.append(f"`{name}` — `{hexcol}` (priorité {pri})")

    await ctx.send(embed=make_embed(
        "🎭 Rôles WorldManager",
        "\n".join(lines),
        color=0x00B4DB))


# ── !kick ─────────────────────────────────────────────────────────────────────
@bot.command(name="kick")
async def cmd_kick(ctx: commands.Context, player: str = None,
                   *, reason: str = "Kicked by admin"):
    if player is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!kick <pseudo> [raison]`",
            color=0xE74C3C))
        return
    mc_cmd(f"kick {player} {reason}")
    await ctx.send(embed=make_embed(
        "👢 Kick",
        f"**{player}** expulsé. Raison: `{reason}`",
        color=0xE67E22))


# ── !ban ──────────────────────────────────────────────────────────────────────
@bot.command(name="ban")
async def cmd_ban(ctx: commands.Context, player: str = None,
                  *, reason: str = "Banned by admin"):
    if player is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!ban <pseudo> [raison]`",
            color=0xE74C3C))
        return
    mc_cmd(f"ban {player} {reason}")
    await ctx.send(embed=make_embed(
        "🔨 Ban",
        f"**{player}** banni. Raison: `{reason}`",
        color=0xE74C3C))


# ── !unban ────────────────────────────────────────────────────────────────────
@bot.command(name="unban", aliases=["pardon"])
async def cmd_unban(ctx: commands.Context, player: str = None):
    if player is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!unban <pseudo>`", color=0xE74C3C))
        return
    mc_cmd(f"pardon {player}")
    await ctx.send(embed=make_embed(
        "✅ Unban", f"**{player}** débanni.", color=0x2ECC71))


# ── !op / !deop ───────────────────────────────────────────────────────────────
@bot.command(name="op")
async def cmd_op(ctx: commands.Context, player: str = None):
    if player is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!op <pseudo>`", color=0xE74C3C))
        return
    mc_cmd(f"op {player}")
    await ctx.send(embed=make_embed(
        "👑 OP", f"**{player}** est maintenant OP.",
        color=0xF1C40F))


@bot.command(name="deop")
async def cmd_deop(ctx: commands.Context, player: str = None):
    if player is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!deop <pseudo>`", color=0xE74C3C))
        return
    mc_cmd(f"deop {player}")
    await ctx.send(embed=make_embed(
        "🚫 DeOP", f"**{player}** n'est plus OP.",
        color=0xE74C3C))


# ── !say ──────────────────────────────────────────────────────────────────────
@bot.command(name="say")
async def cmd_say(ctx: commands.Context, *, message: str = None):
    if message is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!say <message>`", color=0xE74C3C))
        return
    mc_cmd(f"say [Admin] {message}")
    await ctx.send(embed=make_embed(
        "💬 Message envoyé", f"`{message}`", color=0x3498DB))


# ── !broadcast ────────────────────────────────────────────────────────────────
@bot.command(name="broadcast", aliases=["bc"])
async def cmd_broadcast(ctx: commands.Context, *, message: str = None):
    if message is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!broadcast <message>`", color=0xE74C3C))
        return
    mc_cmd(f"broadcast {message}")
    await ctx.send(embed=make_embed(
        "📢 Broadcast", f"`{message}`", color=0x9B59B6))


# ── !tp ───────────────────────────────────────────────────────────────────────
@bot.command(name="tp")
async def cmd_tp(ctx: commands.Context,
                 player: str = None, target: str = None):
    if player is None or target is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!tp <joueur> <destination>`",
            color=0xE74C3C))
        return
    mc_cmd(f"tp {player} {target}")
    await ctx.send(embed=make_embed(
        "⚡ Téléportation",
        f"**{player}** → **{target}**", color=0x1ABC9C))


# ── !give ─────────────────────────────────────────────────────────────────────
@bot.command(name="give")
async def cmd_give(ctx: commands.Context,
                   player: str = None, item: str = None,
                   amount: str = "1"):
    if player is None or item is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!give <joueur> <item> [quantite]`",
            color=0xE74C3C))
        return
    mc_cmd(f"give {player} {item} {amount}")
    await ctx.send(embed=make_embed(
        "🎁 Give",
        f"`{amount}x {item}` → **{player}**",
        color=0x2ECC71))


# ── !gamemode ─────────────────────────────────────────────────────────────────
@bot.command(name="gamemode", aliases=["gm"])
async def cmd_gamemode(ctx: commands.Context,
                       player: str = None, mode: str = None):
    modes = {
        "c": "creative", "s": "survival",
        "sp": "spectator", "a": "adventure",
        "creative": "creative", "survival": "survival",
        "spectator": "spectator", "adventure": "adventure",
        "0": "survival", "1": "creative",
        "2": "adventure", "3": "spectator"
    }
    if player is None or mode is None:
        await ctx.send(embed=make_embed(
            "❌",
            "Usage: `!gamemode <joueur> <creative|survival|spectator|adventure>`",
            color=0xE74C3C))
        return
    resolved = modes.get(mode.lower(), mode)
    mc_cmd(f"gamemode {resolved} {player}")
    await ctx.send(embed=make_embed(
        "🎮 Gamemode",
        f"**{player}** → `{resolved}`", color=0x3498DB))


# ── !time ─────────────────────────────────────────────────────────────────────
@bot.command(name="time")
async def cmd_time(ctx: commands.Context, value: str = None):
    if value is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!time <day|night|noon|midnight>`",
            color=0xE74C3C))
        return
    mc_cmd(f"time set {value}")
    await ctx.send(embed=make_embed(
        "🕐 Heure", f"Définie à `{value}`.", color=0xF39C12))


# ── !weather ──────────────────────────────────────────────────────────────────
@bot.command(name="weather")
async def cmd_weather(ctx: commands.Context, value: str = None):
    if value is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!weather <clear|rain|thunder>`",
            color=0xE74C3C))
        return
    mc_cmd(f"weather {value}")
    await ctx.send(embed=make_embed(
        "🌤 Météo", f"Définie à `{value}`.", color=0x3498DB))


# ── !difficulty ───────────────────────────────────────────────────────────────
@bot.command(name="difficulty")
async def cmd_difficulty(ctx: commands.Context, value: str = None):
    if value is None:
        await ctx.send(embed=make_embed(
            "❌",
            "Usage: `!difficulty <peaceful|easy|normal|hard>`",
            color=0xE74C3C))
        return
    mc_cmd(f"difficulty {value}")
    await ctx.send(embed=make_embed(
        "⚔️ Difficulté", f"Définie à `{value}`.", color=0xE67E22))


# ── !whitelist ────────────────────────────────────────────────────────────────
@bot.command(name="whitelist", aliases=["wl"])
async def cmd_whitelist(ctx: commands.Context,
                        action: str = None,
                        player: str = None):
    if action is None:
        await ctx.send(embed=make_embed(
            "❌",
            "Usage: `!whitelist <add|remove|on|off|list> [joueur]`",
            color=0xE74C3C))
        return
    if action in ("add", "remove"):
        if player is None:
            await ctx.send(embed=make_embed(
                "❌", f"Usage: `!whitelist {action} <joueur>`",
                color=0xE74C3C))
            return
        mc_cmd(f"whitelist {action} {player}")
        await ctx.send(embed=make_embed(
            "📋 Whitelist",
            f"`{action}` → **{player}**", color=0x2ECC71))
    elif action in ("on", "off", "list"):
        mc_cmd(f"whitelist {action}")
        await ctx.send(embed=make_embed(
            "📋 Whitelist", f"`{action}` appliqué.", color=0x2ECC71))
    else:
        await ctx.send(embed=make_embed(
            "❌", "Actions: `add`, `remove`, `on`, `off`, `list`",
            color=0xE74C3C))


# ── !heal ─────────────────────────────────────────────────────────────────────
@bot.command(name="heal")
async def cmd_heal(ctx: commands.Context, player: str = None):
    if player is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!heal <pseudo>`", color=0xE74C3C))
        return
    mc_cmd(f"heal {player}")
    await ctx.send(embed=make_embed(
        "❤️ Soin", f"**{player}** soigné.", color=0xE74C3C))


# ── !fly ──────────────────────────────────────────────────────────────────────
@bot.command(name="fly")
async def cmd_fly(ctx: commands.Context, player: str = None):
    if player is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!fly <pseudo>`", color=0xE74C3C))
        return
    mc_cmd(f"fly {player}")
    await ctx.send(embed=make_embed(
        "✈️ Vol", f"Vol togglé pour **{player}**.",
        color=0x3498DB))


# ── !clear ────────────────────────────────────────────────────────────────────
@bot.command(name="clear")
async def cmd_clear(ctx: commands.Context, player: str = None):
    if player is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!clear <pseudo>`", color=0xE74C3C))
        return
    mc_cmd(f"clear {player}")
    await ctx.send(embed=make_embed(
        "🗑️ Clear", f"Inventaire de **{player}** vidé.",
        color=0x95A5A6))


# ── !sudo ─────────────────────────────────────────────────────────────────────
@bot.command(name="sudo")
async def cmd_sudo(ctx: commands.Context, *, command: str = None):
    if command is None:
        await ctx.send(embed=make_embed(
            "❌", "Usage: `!sudo <commande>`", color=0xE74C3C))
        return
    mc_cmd(command)
    await ctx.send(embed=make_embed(
        "💻 Console", f"`{command}`", color=0x2C3E50))


# ── !logs ─────────────────────────────────────────────────────────────────────
@bot.command(name="logs")
async def cmd_logs(ctx: commands.Context, nb: int = 20):
    """Derniers logs MC."""
    if not os.path.isfile(MC_LOG):
        await ctx.send(embed=make_embed(
            "❌", f"Log introuvable: `{MC_LOG}`",
            color=0xE74C3C))
        return
    nb = min(max(nb, 1), 50)
    try:
        result = subprocess.run(
            ["tail", f"-{nb}", MC_LOG],
            capture_output=True, text=True, timeout=5)
        content = result.stdout.strip()
        if not content:
            await ctx.send(embed=make_embed(
                "📋 Logs", "Log vide.", color=0x95A5A6))
            return
        if len(content) > 1900:
            content = "..." + content[-1897:]
        await ctx.send(f"```\n{content}\n```")
    except Exception as e:
        await ctx.send(embed=make_embed(
            "❌ Erreur", f"`{e}`", color=0xE74C3C))


# ── !help_mc ──────────────────────────────────────────────────────────────────
@bot.command(name="help_mc", aliases=["cmds", "commands", "aide"])
async def cmd_help(ctx: commands.Context):
    """Liste des commandes."""
    embed = discord.Embed(
        title="🎮 Commandes Bot Minecraft",
        color=0x00B4DB
    )
    embed.add_field(name="🖥️ Serveur",
        value=(
            "`!start` Lancer\n"
            "`!restart` Redémarrer\n"
            "`!stop` Arrêter\n"
            "`!status` Statut complet\n"
            "`!tps` TPS\n"
            "`!logs [nb]` Logs"
        ), inline=True)
    embed.add_field(name="👥 Joueurs",
        value=(
            "`!players` En ligne\n"
            "`!kick <p> [r]` Kick\n"
            "`!ban <p> [r]` Ban\n"
            "`!unban <p>` Unban\n"
            "`!op / !deop <p>` OP\n"
            "`!whitelist ...`"
        ), inline=True)
    embed.add_field(name="⚙️ Actions",
        value=(
            "`!tp <j> <dst>` TP\n"
            "`!gamemode <j> <m>` GM\n"
            "`!give <j> <i> [q]` Give\n"
            "`!heal / !fly / !clear`\n"
            "`!time / !weather`\n"
            "`!difficulty`"
        ), inline=True)
    embed.add_field(name="🎭 Rôles",
        value=(
            "`!role [@membre]` Voir rôle\n"
            "`!roles` Liste des rôles\n"
            "Apparaît en jeu dans le chat !"
        ), inline=True)
    embed.add_field(name="💬 Chat",
        value=(
            "`!say <msg>` Message MC\n"
            "`!broadcast <msg>` Broadcast\n"
            "`!sudo <cmd>` Console\n"
            "📝 Écrire normalement = message in-game"
        ), inline=True)
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
# ON READY
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    print(f"[Bot] Connecte: {bot.user}", flush=True)
    flush_queue.start()
    asyncio.create_task(event_reader())
    # Status du bot
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="le Build Server ⬡"
        )
    )


bot.run(TOKEN)
