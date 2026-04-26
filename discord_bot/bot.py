import discord
import aiohttp
import os
from discord.ext import commands
from discord import app_commands

TOKEN   = os.environ["DISCORD_BOT_TOKEN"]
GUILD   = int(os.environ["DISCORD_GUILD_ID"])
PAT     = os.environ["GITHUB_PAT"]
REPO    = os.environ["GITHUB_REPO"]

intents = discord.Intents.default()
bot     = commands.Bot(command_prefix="!", intents=intents)
tree    = bot.tree

@tree.command(
    name="restart",
    description="Relancer le serveur Minecraft",
    guild=discord.Object(id=GUILD))
async def restart(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    url = (f"https://api.github.com/repos/{REPO}"
           f"/actions/workflows/server.yml/dispatches")
    headers = {
        "Authorization": f"token {PAT}",
        "Accept": "application/vnd.github.v3+json"
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers,
                          json={"ref": "main"}) as r:
            if r.status == 204:
                await interaction.followup.send(
                    "✅ Serveur relancé !", ephemeral=True)
            else:
                await interaction.followup.send(
                    f"❌ Erreur {r.status}", ephemeral=True)

@tree.command(
    name="status",
    description="Statut du serveur",
    guild=discord.Object(id=GUILD))
async def status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    url = (f"https://api.github.com/repos/{REPO}"
           f"/actions/workflows/server.yml/runs"
           f"?status=in_progress&per_page=1")
    headers = {
        "Authorization": f"token {PAT}",
        "Accept": "application/vnd.github.v3+json"
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            data = await r.json()
            runs = data.get("workflow_runs", [])
            if runs:
                run = runs[0]
                await interaction.followup.send(
                    f"🟢 **En ligne** depuis "
                    f"`{run['created_at'][:16].replace('T',' ')}`",
                    ephemeral=True)
            else:
                await interaction.followup.send(
                    "🔴 **Hors ligne**", ephemeral=True)

@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD))
    print(f"Bot prêt: {bot.user}")

bot.run(TOKEN)
