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
RE_CHAT = re.compile(
    r'INFO\]:\s+<(\w+)>\s+(.*)')
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

        # Chat
        m = RE_CHAT.search(line)
        if m:
            emit({
                "type":   "chat",
                "player": m.group(1),
                "text":   m.group(2)
            })
            continue

        # Mort
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
