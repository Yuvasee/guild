# pyright: basic
"""Runs on a remote host via `ssh host python3 -`: stdlib only, read-only, prints one JSON document.

Kept free of bridge imports because it is shipped as source over ssh and nothing is installed remotely.
"""

import glob
import json
import os
import subprocess
import time

TAIL_BYTES = 1_500_000
SNIPPET = 400
HARNESS = (
    "<task-notification>", "<system-reminder>", "<local-command", "<command-name>", "<command-message>",
    "Caveat:", "<environment_context>", "<user_instructions>", "<turn_aborted>",
)  # fmt: skip
HOME = os.path.expanduser("~")
# An editor left open in a pane for weeks is not work in progress.
NOT_A_RUN = ("vi", "vim", "nvim", "nano", "emacs", "less", "more", "man", "top", "htop", "btop", "tmux")


def main():
    panes = list_panes()
    if panes is None:
        # tmux did not answer: an empty list would look like "no sessions" and wipe the cache.
        print(
            json.dumps({"now": int(time.time() * 1000), "complete": False, "error": "tmux unavailable", "sessions": []})
        )
        return
    tree, argv = read_processes()
    unreadable = [p for p in panes if int(p["pid"]) not in argv and os.path.exists(f"/proc/{p['pid']}")]
    if unreadable:
        # A pane whose process we could not read would silently vanish from the inventory.
        failure = {
            "now": int(time.time() * 1000),
            "complete": False,
            "error": "unreadable pane process",
            "sessions": [],
        }
        print(json.dumps(failure))
        return
    sessions = []
    found = []
    for pane in panes:
        agent = find_agent(int(pane["pid"]), tree, argv)
        if agent is not None:
            found.append((pane, agent))
    # Without a session id in argv, a rollout can be attributed only if no other codex shares its directory.
    codex_dirs = [pane["cwd"] for pane, (kind, _) in found if kind == "codex"]
    for pane, (kind, pid) in found:
        row = {
            "tmux": pane["session"], "tmux_id": pane["session_id"], "pane": pane["pane"], "cwd": pane["cwd"],
            "pid": pid, "started_at": process_start_ms(pid), "agent": kind,
        }  # fmt: skip
        if kind == "claude":
            row.update(describe_claude(pid))
        else:
            row.update(describe_codex(pid, argv[pid], pane["cwd"], codex_dirs.count(pane["cwd"]) == 1))
        row["branch"], row["origin"] = read_git(row.get("cwd") or pane["cwd"])
        sessions.append(row)
    runs = list_runs(panes, tree, argv, {pane["pane"] + pane["session_id"]: agent for pane, (_, agent) in found})
    print(json.dumps({"now": int(time.time() * 1000), "complete": True, "sessions": sessions, "runs": runs}))


def list_runs(panes, children, argv, agent_panes):
    """Panes running something besides an agent, such as a load test; also beside an agent in the same pane."""
    runs = []
    for pane in panes:
        root = int(pane["pid"])
        agent = agent_panes.get(pane["pane"] + pane["session_id"])
        if agent == root:
            continue
        args = argv.get(root, [])
        is_shell = bool(args) and os.path.basename(args[0]).lstrip("-") in ("sh", "bash", "zsh", "dash", "fish")
        for pid in children.get(root, []) if is_shell else [root]:
            if agent is not None and holds_pid(pid, agent, children):
                continue
            words = [os.path.basename(a)[:30] for a in argv.get(pid, [])[:3] if not a.startswith("-")]
            if not words or words[0] in NOT_A_RUN:
                continue
            runs.append(
                {
                    "tmux": pane["session"], "tmux_id": pane["session_id"], "pane": pane["pane"],
                    "label": " ".join(words), "started_at": process_start_ms(pid),
                }
            )  # fmt: skip
            break
    return runs


def holds_pid(pid, target, children):
    return pid == target or any(holds_pid(kid, target, children) for kid in children.get(pid, []))


def list_panes():
    fmt = "#{session_name}\t#{window_index}.#{pane_index}\t#{pane_pid}\t#{pane_current_path}\t#{session_id}"
    try:
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", fmt], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return [] if "no server running" in out.stderr else None
    panes = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 5 and parts[2].isdigit():
            panes.append(
                {"session": parts[0], "pane": parts[1], "pid": parts[2], "cwd": parts[3], "session_id": parts[4]}
            )
    return panes


def read_processes():
    children, argv = {}, {}
    if not os.path.isdir("/proc"):
        return children, argv
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                args = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry))
        argv[int(entry)] = args
    return children, argv


def process_env(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            items = f.read().split(b"\0")
    except OSError:
        return {}
    env = {}
    for item in items:
        key, sep, value = item.decode(errors="replace").partition("=")
        if sep:
            env[key] = value
    return env


def process_start_ms(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as f:
            boot = next(int(line.split()[1]) for line in f if line.startswith("btime"))
    except (OSError, ValueError, StopIteration):
        return None
    return boot * 1000 + ticks * 1000 // os.sysconf("SC_CLK_TCK")


def find_agent(root, children, argv):
    """The first claude or codex process under a tmux pane, breadth first."""
    queue = [root]
    while queue:
        pid = queue.pop(0)
        args = argv.get(pid, [])
        if args:
            name = os.path.basename(args[0])
            if name == "claude" or (name == "node" and len(args) > 1 and args[1].endswith("/claude")):
                return "claude", pid
            if name == "codex" or (name == "node" and len(args) > 1 and args[1].endswith("/codex")):
                return "codex", pid
        queue.extend(children.get(pid, []))
    return None


def describe_claude(pid):
    info = {}
    try:
        with open(f"{HOME}/.claude/sessions/{pid}.json") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"state": "unknown"}
    sid = data.get("sessionId")
    info["conversation_id"] = sid
    info["cwd"] = data.get("cwd")
    # "shell" is Claude's own word for a turn that ended with a shell still running.
    info["state"] = "working" if data.get("status") in ("busy", "shell") else "finished"
    info["activity_at"] = data.get("statusUpdatedAt")
    paths = glob.glob(f"{HOME}/.claude/projects/*/{sid}.jsonl") if sid else []
    if paths:
        info["prompt"], info["reply"] = last_turn(paths[0], claude_entry)
        info["activity_at"] = max(info.get("activity_at") or 0, int(os.path.getmtime(paths[0]) * 1000))
    return info


def describe_codex(pid, args, cwd, unique_in_dir):
    sid = args[args.index("resume") + 1] if "resume" in args and args.index("resume") + 1 < len(args) else None
    path = None
    codex_home = process_env(pid).get("CODEX_HOME") or f"{HOME}/.codex"
    if sid:
        found = glob.glob(f"{codex_home}/sessions/*/*/*/rollout-*{sid}.jsonl")
        path = max(found, key=os.path.getmtime) if found else None
    if path is None and unique_in_dir:
        path = newest_rollout_for(codex_home, cwd, process_start_ms(pid) or 0)
    if path is None:
        return {"state": "unknown"}
    info = {"conversation_id": sid or os.path.basename(path)[-41:-6], "activity_at": int(os.path.getmtime(path) * 1000)}
    info["prompt"], info["reply"] = last_turn(path, codex_entry)
    info["state"] = codex_state(path)
    return info


def newest_rollout_for(codex_home, cwd, started_ms):
    candidates = sorted(glob.glob(f"{codex_home}/sessions/*/*/*/rollout-*.jsonl"), key=os.path.getmtime)[-40:]
    for path in reversed(candidates):
        if os.path.getmtime(path) * 1000 < started_ms:
            break
        try:
            with open(path) as f:
                meta = json.loads(f.readline()).get("payload", {})
        except (OSError, ValueError):
            continue
        if meta.get("cwd") == cwd:
            return path
    return None


def codex_state(path):
    state = "unknown"
    for entry in tail_entries(path):
        payload = entry.get("payload") or {}
        if entry.get("type") == "event_msg" and payload.get("type") in (
            "task_started",
            "task_complete",
            "turn_aborted",
        ):
            state = "working" if payload.get("type") == "task_started" else "finished"
    return state


def last_turn(path, extract):
    prompt = reply = None
    for entry in tail_entries(path):
        role, text = extract(entry)
        if not text:
            continue
        if role == "user" and not text.lstrip().startswith(HARNESS):
            prompt = text
        elif role == "assistant":
            reply = text
    return snippet(prompt), snippet(reply)


def claude_entry(entry):
    if entry.get("isMeta") or entry.get("isSidechain"):
        return None, None
    message = entry.get("message") or {}
    return entry.get("type"), text_of(message.get("content"), "text")


def codex_entry(entry):
    payload = entry.get("payload") or {}
    if entry.get("type") != "response_item" or payload.get("type") != "message":
        return None, None
    kind = "input_text" if payload.get("role") == "user" else "output_text"
    return payload.get("role"), text_of(payload.get("content"), kind)


def text_of(content, kind):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == kind]
    return "\n".join(t for t in texts if t) or None


def tail_entries(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return []
    if size > TAIL_BYTES:
        lines = lines[1:]
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def snippet(text):
    if not text:
        return None
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= SNIPPET else collapsed[: SNIPPET - 1] + "…"


def read_git(cwd):
    directory = cwd
    while directory and directory != "/":
        dot_git = os.path.join(directory, ".git")
        if os.path.exists(dot_git):
            git_dir = dot_git
            if os.path.isfile(dot_git):
                with open(dot_git) as f:
                    git_dir = os.path.join(directory, f.read().split("gitdir:", 1)[-1].strip())
            branch = None
            try:
                with open(os.path.join(git_dir, "HEAD")) as f:
                    head = f.read().strip()
                branch = head.removeprefix("ref: refs/heads/") if head.startswith("ref: ") else head[:12]
            except OSError:
                pass
            common = git_dir
            if os.path.isfile(os.path.join(git_dir, "commondir")):
                with open(os.path.join(git_dir, "commondir")) as f:
                    common = os.path.normpath(os.path.join(git_dir, f.read().strip()))
            return branch, origin_url(common)
        directory = os.path.dirname(directory)
    return None, None


def origin_url(common):
    try:
        with open(os.path.join(common, "config")) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped.replace(" ", "") == '[remote"origin"]'
        elif inside and stripped.startswith("url"):
            return stripped.split("=", 1)[1].strip()
    return None


main()
