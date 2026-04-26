#!/usr/bin/env python3
"""
Lit mc.log en temps reel et ecrit des evenements JSON
dans /tmp/mc_events.jsonl lu par discord_bot.py
"""
import sys, os, time, re, json

MC_LOG     = sys.argv[1] if len(sys.argv) > 1 else "mc.log"
EVENT_PIPE = os.environ.get("MC_EVENT_PIPE", "/tmp/mc_events.jsonl")

RE_JOIN  = re.compile(r'INFO\]:\s+(\w+)\[/.+\]\s+logged in with entity id')
RE_QUIT  = re.compile(r'INFO\]:\s+(\w+)\s+lost connection:')
RE_CHAT  = re.compile(r'INFO\]:\s+<(\w+)>\s+(.*)')
RE_DEATH = re.compile(
    r'INFO\]:\s+(.+?(?:was slain|drowned|fell from|was burnt|'
    r'starved|suffocated|killed by|hit the ground|blew up).*)',
    re.IGNORECASE
)

def emit(ev: dict):
    line = json.dumps(ev, ensure_ascii=False)
    try:
        with open(EVENT_PIPE, "w") as f:
            f.write(line + "\n")
            f.flush()
        print(f"[Watcher] emit: {line}", flush=True)
    except Exception as e:
        print(f"[Watcher] Erreur emit: {e}", flush=True)

def emit_raw(ev: dict):
    """Ouvre le pipe en mode append pour ne pas perdre d'events."""
    line = json.dumps(ev, ensure_ascii=False)
    try:
        with open(EVENT_PIPE, "a") as f:
            f.write(line + "\n")
            f.flush()
        print(f"[Watcher] emit: {line}", flush=True)
    except Exception as e:
        print(f"[Watcher] Erreur emit: {e}", flush=True)

# Attendre le log
waited = 0
while not os.path.isfile(MC_LOG):
    time.sleep(1)
    waited += 1
    if waited > 120:
        print("[Watcher] Timeout attente mc.log", flush=True)
        sys.exit(0)

print(f"[Watcher] Surveillance de {MC_LOG}", flush=True)

with open(MC_LOG, "r", errors="replace") as f:
    f.seek(0, 2)  # aller a la fin
    while True:
        line = f.readline()
        if not line:
            time.sleep(0.15)
            continue
        line = line.rstrip()

        m = RE_JOIN.search(line)
        if m:
            emit_raw({"type": "join", "player": m.group(1)})
            continue

        m = RE_QUIT.search(line)
        if m:
            emit_raw({"type": "quit", "player": m.group(1)})
            continue

        m = RE_CHAT.search(line)
        if m:
            emit_raw({"type": "chat", "player": m.group(1), "text": m.group(2)})
            continue

        m = RE_DEATH.search(line)
        if m:
            emit_raw({"type": "death", "text": m.group(1)})
            continue
