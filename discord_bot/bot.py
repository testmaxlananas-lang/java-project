import discord
import aiohttp
import asyncio
import os
import json
from discord.ext import commands, tasks
from discord import app_commands

TOKEN      = os.environ["DISCORD_BOT_TOKEN"]
GUILD      = int(os.environ["DISCORD_GUILD_ID"])
CHANNEL    = int(os.environ["DISCORD_CHANNEL_ID"])
PAT        = os.environ["GITHUB_PAT"]
REPO       = os.environ["GITHUB_REPO"]
EVENT_PIPE = os.environ.get("MC_EVENT_PIPE", "/tmp/mc_events.jsonl")

intents         = discord.Intents.default()
intents.message_content = True
bot             = commands.Bot(command_prefix="!", intents=intents)
tree            = bot.tree

# ── File d'attente des messages Discord ──────────────────────────────────────
msg_queue: asyncio.Queue = asyncio.Queue()

# ── Lecture du pipe d'events MC ─────────────────────────────────────────────
async def pipe_reader():
    """
    Lit /tmp/mc_events.jsonl en continu.
    Chaque ligne = un JSON  {"type": "chat", "player": "...", "text": "..."}
    """
    loop = asyncio.get_event_loop()

    # Créer le pipe s'il n'existe pas
    if not os.path.exists(EVENT_PIPE):
        try:
            os.mkfifo(EVENT_PIPE)
        except FileExistsError:
            pass

    print(f"[Bot] Lecture pipe: {EVENT_PIPE}", flush=True)

    while True:
        try:
            # Ouvrir en mode non-bloquant
            fd = await loop.run_in_executor(
                None,
                lambda: open(EVENT_PIPE, "r", errors="replace")
            )
            async for raw in async_lines(fd, loop):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg = format_event(ev)
                if msg:
                    await msg_queue.put(msg)
            fd.close()
        except Exception as e:
            print(f"[Bot] Pipe erreur: {e}", flush=True)
            await asyncio.sleep(2)


async def async_lines(f, loop):
    """Générateur asynchrone de lignes depuis un fichier."""
    while True:
        line = await loop.run_in_executor(None, f.readline)
        if line:
            yield line
        else:
            await asyncio.sleep(0.1)


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
        player = ev.get("player", "?")
        return f":arrow_right: **{player}** a rejoint le serveur."

    if t == "quit":
        player = ev.get("player", "?")
        return f":arrow_left: **{player}** a quitte le serveur."

    if t == "chat":
        player = ev.get("player", "?")
        text   = ev.get("text", "")
        return f"**{player}** {text}"

    if t == "death":
        text = ev.get("text", "")
        return f":skull_crossbones: {text}"

    return None


# ── Envoi des messages en queue vers Discord ─────────────────────────────────
@tasks.loop(seconds=0.5)
async def flush_queue():
    channel = bot.get_channel(CHANNEL)
    if channel is None:
        return
    batch = []
    while not msg_queue.empty() and len(batch) < 5:
        batch.append(await msg_queue.get())
    if batch:
        content = "\n".join(batch)
        try:
            await channel.send(content)
        except Exception as e:
            print(f"[Bot] Send erreur: {e}", flush=True)


# ── Commandes slash ──────────────────────────────────────────────────────────
@tree.command(
    name="restart",
    description="Relancer le serveur Minecraft",
    guild=discord.Object(id=GUILD)
)
async def cmd_restart(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    url = (f"https://api.github.com/repos/{REPO}"
           f"/actions/workflows/server.yml/dispatches")
    headers = {
        "Authorization": f"token {PAT}",
        "Accept":        "application/vnd.github.v3+json"
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, json={"ref": "main"}) as r:
            if r.status == 204:
                await interaction.followup.send(
                    ":white_check_mark: Serveur relance !", ephemeral=True)
            else:
                await interaction.followup.send(
                    f":x: Erreur {r.status}", ephemeral=True)


@tree.command(
    name="status",
    description="Statut du serveur",
    guild=discord.Object(id=GUILD)
)
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    url = (f"https://api.github.com/repos/{REPO}"
           f"/actions/workflows/server.yml/runs"
           f"?status=in_progress&per_page=1")
    headers = {
        "Authorization": f"token {PAT}",
        "Accept":        "application/vnd.github.v3+json"
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            data = await r.json()
            runs = data.get("workflow_runs", [])
            if runs:
                run = runs[0]
                ts  = run["created_at"][:16].replace("T", " ")
                await interaction.followup.send(
                    f":green_circle: **En ligne** depuis `{ts} UTC`",
                    ephemeral=True)
            else:
                await interaction.followup.send(
                    ":red_circle: **Hors ligne**", ephemeral=True)


# ── Events bot ───────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD))
    print(f"[Bot] Connecte: {bot.user}", flush=True)
    flush_queue.start()
    asyncio.create_task(pipe_reader())


bot.run(TOKEN)
