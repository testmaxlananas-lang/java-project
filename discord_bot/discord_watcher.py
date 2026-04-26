#!/usr/bin/env python3
"""
Surveille le log MC et émet des événements JSON vers EVENT_FILE.
"""
import sys
import os
import time
import re
import json

MC_LOG     = sys.argv[1] if len(sys.argv) > 1 else "mc.log"
EVENT_FILE = os.environ.get("MC_EVENT_FILE", "/tmp/mc_events.jsonl")

# ── Patterns ──────────────────────────────────────────────────────────────────
RE_JOIN = re.compile(
    r'INFO\]:\s+(\w+)\[/.+\]\s+logged in with entity id')
RE_QUIT = re.compile(
    r'INFO\]:\s+(\w+)\s+lost connection:')

# Format vanilla : <Pseudo> message
RE_CHAT_VANILLA = re.compile(
    r'INFO\]:\s+<(\w+)>\s+(.*)')

# Format custom avec préfixe de rôle (emoji, texte, séparateur)
# Ex: "☩ Grandmaster ・ MaxLananas > test"
# Ex: "🌑 Mantled ・ Bob > hello"
# Capture le pseudo (dernier mot avant " > ") et le message
RE_CHAT_CUSTOM = re.compile(
    r'INFO\]:\s+.+?[・•\|]\s*(\w+)\s*[>\:]\s+(.*)')

# Fallback encore plus large : mot avant " > message"
# pour les formats sans séparateur unicode
RE_CHAT_ARROW = re.compile(
    r'INFO\]:\s+(?:\S+\s+)*?(\w+)\s+>\s+(.*)')

RE_DEATH = re.compile(
    r'INFO\]:\s+(.+?(?:'
    r'was slain|drowned|fell from|was burnt|'
    r'starved to death|suffocated|killed by|'
    r'hit the ground|blew up|was shot|'
    r'was impaled|was fireballed|'
    r'fell out of the world'
    r').*)',
    re.IGNORECASE
)
RE_CMD = re.compile(
    r'INFO\]:\s+(\w+) issued server command:\s+(.*)')
RE_ADV = re.compile(
    r'INFO\]:\s+(\w+) has made the advancement \[(.+)\]')
RE_CHALLENGE = re.compile(
    r'INFO\]:\s+(\w+) has completed the challenge \[(.+)\]')


def emit(ev: dict) -> None:
    line = json.dumps(ev, ensure_ascii=False)
    try:
        with open(EVENT_FILE, "a") as f:
            f.write(line + "\n")
            f.flush()
        print(f"[Watcher] {line}", flush=True)
    except Exception as e:
        print(f"[Watcher] Erreur emit: {e}", flush=True)


def try_parse_chat(line: str) -> dict | None:
    """
    Tente de détecter un message de chat dans plusieurs formats.
    Retourne un dict {"player": ..., "text": ...} ou None.
    """
    # 1. Format vanilla <Pseudo> message
    m = RE_CHAT_VANILLA.search(line)
    if m:
        return {"player": m.group(1), "text": m.group(2)}

    # 2. Format custom avec séparateur ・ ou • ou |
    #    Ex: "☩ Grandmaster ・ MaxLananas > test"
    m = RE_CHAT_CUSTOM.search(line)
    if m:
        return {"player": m.group(1), "text": m.group(2)}

    # 3. Fallback flèche simple "Pseudo > message"
    #    (évite les faux positifs avec les logs système)
    m = RE_CHAT_ARROW.search(line)
    if m and not any(kw in line for kw in [
        "logged in", "lost connection", "issued server command",
        "has made", "has completed", "GameProfile", "UUID",
        "Starting", "Stopping", "Loading", "Saving"
    ]):
        return {"player": m.group(1), "text": m.group(2)}

    return None


# ── Attente log ───────────────────────────────────────────────────────────────
waited = 0
while not os.path.isfile(MC_LOG):
    time.sleep(1)
    waited += 1
    if waited > 120:
        print("[Watcher] Timeout attente mc.log", flush=True)
        sys.exit(0)

print(f"[Watcher] Surveillance: {MC_LOG}", flush=True)

# ── Boucle principale ─────────────────────────────────────────────────────────
with open(MC_LOG, "r", errors="replace") as f:
    f.seek(0, 2)  # Aller à la fin
    while True:
        line = f.readline()
        if not line:
            time.sleep(0.1)
            continue
        line = line.rstrip()
        if not line:
            continue

        # Join
        m = RE_JOIN.search(line)
        if m:
            emit({"type": "join", "player": m.group(1)})
            continue

        # Quit
        m = RE_QUIT.search(line)
        if m:
            emit({"type": "quit", "player": m.group(1)})
            continue

        # Mort (avant chat pour éviter faux positifs)
        m = RE_DEATH.search(line)
        if m:
            emit({"type": "death", "text": m.group(1)})
            continue

        # Avancement
        m = RE_ADV.search(line)
        if m:
            emit({
                "type":        "advancement",
                "player":      m.group(1),
                "advancement": m.group(2)
            })
            continue

        # Challenge
        m = RE_CHALLENGE.search(line)
        if m:
            emit({
                "type":      "challenge",
                "player":    m.group(1),
                "challenge": m.group(2)
            })
            continue

        # Chat (en dernier pour éviter les faux positifs)
        chat = try_parse_chat(line)
        if chat:
            emit({"type": "chat", **chat})
            continue
