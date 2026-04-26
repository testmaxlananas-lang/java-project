#!/usr/bin/env python3
import sys, os, time, re, json

MC_LOG     = sys.argv[1] if len(sys.argv) > 1 else "mc.log"
EVENT_FILE = os.environ.get("MC_EVENT_FILE", "/tmp/mc_events.jsonl")

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
        with open(EVENT_FILE, "a") as f:
            f.write(line + "\n")
            f.flush()
        print(f"[Watcher] {line}", flush=True)
    except Exception as e:
        print(f"[Watcher] Erreur: {e}", flush=True)

waited = 0
while not os.path.isfile(MC_LOG):
    time.sleep(1)
    waited += 1
    if waited > 120:
        sys.exit(0)

print(f"[Watcher] Surveillance: {MC_LOG}", flush=True)

with open(MC_LOG, "r", errors="replace") as f:
    f.seek(0, 2)
    while True:
        line = f.readline()
        if not line:
            time.sleep(0.15)
            continue
        line = line.rstrip()

        m = RE_JOIN.search(line)
        if m:
            emit({"type": "join", "player": m.group(1)})
            continue

        m = RE_QUIT.search(line)
        if m:
            emit({"type": "quit", "player": m.group(1)})
            continue

        m = RE_CHAT.search(line)
        if m:
            emit({"type": "chat", "player": m.group(1), "text": m.group(2)})
            continue

        m = RE_DEATH.search(line)
        if m:
            emit({"type": "death", "text": m.group(1)})
            continue
