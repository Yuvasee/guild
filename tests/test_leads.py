import io
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from conftest import VENV_BIN, BridgeEnv, SpawnProcess, wait_for

from bridge import hooks
from bridge.compaction import KEEP_MAX_CHARS
from bridge.config import map_toml_to_config
from bridge.inject import INJECT_MAX_CHARS, Part, measure_chars
from bridge.leads import NOTES_INJECT_MAX_CHARS, convert_config_to_assistant_role


class Team:
    def __init__(self, bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
        self.env = bridge_env
        self.a = spawn_process(env={"AGTERM_PANE_ID": "PANE-A", "AGTERM_SESSION_ID": "TAB-A"})
        self.b = spawn_process(env={"AGTERM_PANE_ID": "PANE-B", "AGTERM_SESSION_ID": "TAB-B"})
        bridge_env.write_session_file(self.a, "conv-a")
        bridge_env.write_session_file(self.b, "conv-b")
        self.write_tree()
        self.start(self.a, "conv-a")
        self.start(self.b, "conv-b")
        bridge_env.cli("sessions")

    def write_tree(self, workspace_a: str = "Work", workspace_b: str = "Work", tab_a: str = "Old A") -> None:
        def session(tab: str, sid: str, pane: str) -> dict[str, object]:
            return {"id": sid, "name": tab, "surfaces": [{"paneID": pane}]}

        spaces: dict[str, list[dict[str, object]]] = {}
        spaces.setdefault(workspace_a, []).append(session(tab_a, "TAB-A", "PANE-A"))
        spaces.setdefault(workspace_b, []).append(session("Old B", "TAB-B", "PANE-B"))
        workspaces = [
            {"id": "WS-NEW" if name == "TEAM" else f"WS-{name}", "name": name, "sessions": ss}
            for name, ss in spaces.items()
        ]
        self.env.stub_tree.write_text(json.dumps({"ok": True, "result": {"tree": {"workspaces": workspaces}}}))

    def start(self, pid: int, conv: str, source: str = "startup") -> str:
        payload = {"session_id": conv, "cwd": str(self.env.root), "source": source}
        env = {"AGTERM_PANE_ID": "PANE-A" if pid == self.a else "PANE-B"}
        return _context(self.env.hook("SessionStart", payload, pid, env).stdout)

    def prompt(self, pid: int, conv: str, text: str) -> str:
        payload = {
            "session_id": conv,
            "cwd": str(self.env.root),
            "prompt": text,
            "prompt_id": f"{text}-{time.time_ns()}",
        }
        env = {"AGTERM_PANE_ID": "PANE-A" if pid == self.a else "PANE-B"}
        return _context(self.env.hook("UserPromptSubmit", payload, pid, env).stdout)

    def guild(self, *args: str, pid: int | None = None, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(self.env.env)
        if pid is not None:
            env["CLAUDE_PID"] = str(pid)
        return subprocess.run(
            [str(VENV_BIN / "guild"), *args],
            input=stdin,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def enroll(self, name: str = "Alex", tab: str = "old-a", *extra: str) -> subprocess.CompletedProcess[str]:
        return self.guild("lead", "add", name, "--tab", tab, "--scope", "Data readiness", "--label", "Data", *extra)

    def log(self) -> str:
        return self.env.stub_log.read_text() if self.env.stub_log.exists() else ""


def _context(stdout: str) -> str:
    return json.loads(stdout)["hookSpecificOutput"]["additionalContext"] if stdout.strip() else ""


def test_enroll_names_and_tints_the_tab_and_moves_it_to_the_team_workspace(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)

    done = team.enroll("Alex", "old-a", "--alias", "Arlow", "--ref", "P-PROJ-41", "--tint", "blue")
    mentioned = bridge_env.cli("send", "arlow", "hello", pid=team.b)
    old_name = bridge_env.cli("send", "old-a", "still reachable", pid=team.b)
    listed = json.loads(team.guild("leads", "--json").stdout)

    assert done.returncode == 0, done.stderr
    log = team.log()
    assert "workspace new TEAM --json" in log
    assert "workspace tint" not in log
    assert "session move WS-NEW --target TAB-A" in log
    assert "session rename Alex · Data --target TAB-A" in log
    assert "session tint blue --on icon,name --target TAB-A" in log
    assert "session context Data readiness --target TAB-A" in log
    assert mentioned.returncode == 0, mentioned.stderr
    assert "→ alex" in mentioned.stdout
    assert old_name.returncode == 0, old_name.stderr
    assert [(r["name"], r["status"], r["linear_refs"]) for r in listed] == [("alex", "idle", ["P-PROJ-41"])]


def test_sync_is_idempotent_once_the_tab_is_in_the_team_workspace(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.enroll("Boris", "old-b")
    team.write_tree(workspace_a="TEAM", tab_a="Alex · Data", workspace_b="TEAM")
    bridge_env.stub_log.write_text("")

    done = team.guild("lead", "sync", "alex")

    assert done.returncode == 0, done.stderr
    assert "workspace new" not in team.log()
    assert "session move" not in team.log()


def test_failed_terminal_step_keeps_the_lead_and_says_how_to_retry(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    good = config.read_text()
    config.write_text(good.replace(f'agtermctl = "{bridge_env.root / "agtermctl"}"', 'agtermctl = "/nonexistent"'))

    done = team.enroll()
    config.write_text(good)

    assert done.returncode == 1
    assert "guild lead sync alex" in done.stderr
    assert len(json.loads(team.guild("leads", "--json").stdout)) == 1


def test_locked_tab_cannot_be_enrolled_and_a_tab_locked_later_is_ignored(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.write_tree(workspace_b="🔒 Misc")
    bridge_env.cli("sessions")

    refused = team.enroll("Boris", "old-b")
    enrolled = team.enroll("Alex", "old-a")
    team.write_tree(workspace_a="My 🔒 Space", workspace_b="🔒 Misc")
    bridge_env.cli("sessions")
    role_after_lock = team.start(team.a, "conv-a")
    listed = json.loads(team.guild("leads", "--json").stdout)

    assert refused.returncode == 2
    assert "locked" in refused.stderr
    assert enrolled.returncode == 0, enrolled.stderr
    assert "[guild]" not in role_after_lock
    assert listed[0]["status"] == "locked"


def test_retired_name_is_never_reused_and_the_tab_goes_back(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.write_tree(workspace_a="TEAM", tab_a="Alex · Data")

    retired = team.guild("lead", "retire", "alex")
    again = team.enroll("Alex", "old-b")
    role = team.start(team.a, "conv-a")

    assert retired.returncode == 0, retired.stderr
    assert "session move WS-Work --target TAB-A" in team.log()
    assert "session rename Old A --target TAB-A" in team.log()
    assert "session context --clear --target TAB-A" in team.log()
    assert "session tint clear --target TAB-A" in team.log()
    assert again.returncode == 2
    assert "never reused" in again.stderr
    assert "[guild]" not in role
    assert bridge_env.cli("send", "alex", "x", pid=team.b).returncode != 0
    assert bridge_env.cli("send", "old-a", "back", pid=team.b).returncode == 0
    assert bridge_env.cli("handle", "alex", pid=team.b).returncode != 0


def test_lead_names_and_aliases_cannot_be_taken(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll("Alex", "old-a", "--alias", "Arlow")
    team.write_tree(tab_a="Alex")

    latin = bridge_env.cli("handle", "alex", pid=team.b)
    alias = bridge_env.cli("handle", "arlow", pid=team.b)
    own = bridge_env.cli("handle", "someone", pid=team.a)
    team.write_tree(tab_a="Alex")
    team.env.stub_tree.write_text(team.env.stub_tree.read_text().replace("Old B", "Alex"))
    bridge_env.cli("sessions")
    listed = {s["pid"]: s["handle"] for s in json.loads(bridge_env.cli("sessions", "--json").stdout)}

    assert latin.returncode != 0
    assert alias.returncode != 0
    assert own.returncode != 0
    assert listed[team.a] == "alex"
    assert listed[team.b] == "alex-2"


def test_retirement_does_not_take_a_handle_that_moved_on(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.write_tree(workspace_a="TEAM", tab_a="Alex · Data")
    assert bridge_env.cli("handle", "old-a", pid=team.b).returncode == 0
    assert bridge_env.cli("handle", "other", pid=team.b).returncode == 0

    done = team.guild("lead", "retire", "alex")
    reaches = bridge_env.cli("send", "old-a", "still b", pid=team.a)

    assert done.returncode == 0, done.stderr
    assert "→ other" in reaches.stdout


def test_failed_restore_keeps_the_lead_and_retrying_works(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.write_tree(workspace_a="TEAM", tab_a="Alex · Data", workspace_b="Other")

    gone = team.guild("lead", "retire", "alex")
    still = json.loads(team.guild("leads", "--json").stdout)
    forced = team.guild("lead", "retire", "alex", "--force")

    assert gone.returncode == 1
    assert "workspace Work is gone" in gone.stderr
    assert [r["name"] for r in still] == ["alex"]
    assert forced.returncode == 0, forced.stderr


def test_wake_types_a_first_prompt(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()

    woken = team.guild("lead", "wake", "alex")

    assert woken.returncode == 0, woken.stderr
    assert "you are Alex, a team lead now" in team.log()


def test_one_tab_cannot_be_two_leads_and_names_are_validated(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()

    second = team.enroll("Boris", "alex")
    lowercase = team.enroll("boris", "old-b")
    taken_alias = team.enroll("Boris", "old-b", "--alias", "old-a")

    assert second.returncode == 2
    assert "already is Alex" in second.stderr
    assert lowercase.returncode == 2
    assert taken_alias.returncode == 2


def test_role_comes_with_every_start_and_once_per_conversation(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll("Alex", "old-a", "--ref", "P-PROJ-41")

    running = team.prompt(team.a, "conv-a", "first prompt after enrolling")
    quiet = team.prompt(team.a, "conv-a", "second")
    compacted = team.start(team.a, "conv-a", "compact")
    resumed = team.start(team.a, "conv-a", "resume")
    cleared = team.start(team.a, "conv-a2", "clear")
    other_tab = team.prompt(team.b, "conv-b", "not a lead")

    assert "You are Alex, a team lead" in running
    assert "Data readiness (tracker: P-PROJ-41)" in running
    assert "[guild]" not in quiet
    for start in (compacted, resumed, cleared):
        assert "You are Alex, a team lead" in start
    assert "[guild]" not in other_tab


def test_notes_outlive_the_conversation_and_are_cut_to_budget(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()

    empty = team.start(team.a, "conv-a")
    saved = team.guild("notes", "--write", pid=team.a, stdin="Owns PROJ-33; ✓ ünïcödé.\n")
    with_notes = team.start(team.a, "conv-a2", "clear")
    outsider = team.guild("notes", "--write", pid=team.b, stdin="x")
    too_long = team.guild("notes", "--write", pid=team.a, stdin="é" * 5000)
    long_notes = team.start(team.a, "conv-a2", "resume")

    assert "start here: make an honest pass" in empty
    assert saved.returncode == 0, saved.stderr
    assert "Owns PROJ-33; ✓ ünïcödé." in with_notes
    assert "start here" not in with_notes
    assert outsider.returncode == 2
    assert "over the 4,000 that reach you" in too_long.stdout
    assert "Your notes are 5,000 characters, over the 4,000" in long_notes
    assert "Rewrite them shorter with `guild notes --write`" in long_notes
    (bridge_env.state_dir / "leads" / "alex" / "notes.md").write_bytes(b"caf\xff notes")
    assert "caf\ufffd notes" in team.start(team.a, "conv-a2", "resume")
    assert measure_chars(long_notes) <= INJECT_MAX_CHARS


def test_notes_over_the_limit_reach_the_lead_from_their_newest_end(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    lines = [f"- Rule {i:03}: answer on the issue, relayed by the assistant, before anything else." for i in range(200)]
    team.guild("notes", "--write", pid=team.a, stdin="\n".join(lines) + "\n")

    notes = team.start(team.a, "conv-a2", "clear").split("Their last ", 1)[1]

    assert "- Rule 199:" in notes
    assert "- Rule 000:" not in notes
    assert notes.split("\n", 1)[1].startswith("- Rule "), "the cut starts at a whole line"
    assert measure_chars(notes) <= NOTES_INJECT_MAX_CHARS + 50


def test_the_worst_case_stays_under_claude_codes_limit_and_nothing_waits_unsaid(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    scope = ("Search quality: indexing, ranking, PROJ-33, PROJ-53; " * 10)[:500]
    refs = [arg for i in range(10) for arg in ("--ref", f"P-PROJ-{40 + i}")]
    assert team.enroll("Alex", "old-a", "--scope", scope, *refs).returncode == 0
    notes = "".join(f"- Rule ({i:03}, Theo via Xavi): no merge while a thread is open ✓ 🤖.\n" for i in range(240))
    assert len(notes.encode()) > 16_000
    team.guild("notes", "--write", pid=team.a, stdin=notes)
    # Mid-turn, so the mail cannot be typed: an idle stub tab lets the typer win the race against the hooks.
    rule = chr(0x2500) * 2
    busy = f"{chr(0xB7)} Compacting{chr(0x2026)} (5s)\n{rule}\n{chr(0x276F)}{chr(0xA0)}\n{rule}\n"
    Path(bridge_env.env["STUB_DRAFT"]).write_text(busy)
    bridge_env.cli("send", "alex", "PROJ-34 is yours now: " + "x" * 900, pid=team.b)
    assert wait_for(lambda: _typing_wanted(bridge_env)), "bridge tries to type mail to a lead"
    keep = ("🤖 PROJ-34 mine, PR acme/web#7 waits ✓\n" * 400)[: KEEP_MAX_CHARS // 2]
    while measure_chars(keep) < KEEP_MAX_CHARS:
        keep += "k"
    over = bridge_env.cli("keep", keep + "k", pid=team.a)
    kept = bridge_env.cli("keep", keep, pid=team.a)

    compacted = team.start(team.a, "conv-a", "compact")
    following = team.prompt(team.a, "conv-a", "[bridge] Compaction is done.")
    after = team.prompt(team.a, "conv-a", "next")

    assert over.returncode == 2
    assert kept.returncode == 0, kept.stderr
    for message in (compacted, following, after):
        assert measure_chars(message) <= INJECT_MAX_CHARS
    assert keep.strip() in compacted, "the saved list comes first and whole"
    assert "Left out of this message to stay under Claude Code's 10,000-character limit" in compacted
    assert "Rule (239" in compacted + following, "the newest notes line reaches the lead"
    assert "PROJ-34 is yours now" in compacted + following + after
    assert "Left out of this message" not in after


def _typing_wanted(bridge_env: BridgeEnv) -> bool:
    with bridge_env.connect() as conn:
        return conn.execute("SELECT typing_wanted_at FROM message_recipients").fetchone()[0] is not None


def test_handover_reaches_the_lead(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()

    done = team.guild("handover", "alex", pid=team.b, stdin="PROJ-53 is yours; PR #7 waits for review.")
    delivered = team.prompt(team.a, "conv-a", "next")
    shown = team.guild("notes", "--handovers", pid=team.a)
    empty = team.guild("handover", "alex", pid=team.b, stdin=" ")

    assert done.returncode == 0, done.stderr
    assert "Handover from old-b" in delivered
    assert "PR #7 waits for review" in shown.stdout
    assert "From old-b" in shown.stdout
    assert empty.returncode == 2


def test_lead_tab_keeps_its_name_while_other_tabs_follow_their_session(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.write_tree(workspace_a="TEAM", tab_a="Alex · Data")
    projects = Path(bridge_env.env["BRIDGE_CLAUDE_PROJECTS_DIR"]) / "p"
    projects.mkdir(parents=True)
    title = json.dumps({"type": "custom-title", "customTitle": "Something else"}) + "\n"
    (projects / "conv-a3.jsonl").write_text(title)
    (projects / "conv-b3.jsonl").write_text(title)
    bridge_env.write_session_file(team.a, "conv-a3")
    bridge_env.write_session_file(team.b, "conv-b3")
    team.start(team.a, "conv-a3", "clear")
    team.start(team.b, "conv-b3", "clear")
    bridge_env.stub_log.write_text("")

    bridge_env.cli("sessions")

    assert "session rename Something else --target TAB-B" in team.log()
    assert "--target TAB-A" not in team.log()


def test_label_names_the_tab_and_can_change(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.write_tree(workspace_a="TEAM", tab_a="Alex · Data")
    bridge_env.stub_log.write_text("")

    changed = team.guild("lead", "label", "alex", "Scale & Load")
    non_english = team.guild("lead", "label", "alex", "Données")

    assert changed.returncode == 0, changed.stderr
    assert "session rename Alex · Scale & Load --target TAB-A" in team.log()
    assert "workspace rename" not in team.log()
    assert non_english.returncode == 2
    assert json.loads(team.guild("leads", "--json").stdout)[0]["label"] == "Scale & Load"


def test_non_english_session_title_is_not_set_and_the_session_is_asked(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    projects = Path(bridge_env.env["BRIDGE_CLAUDE_PROJECTS_DIR"]) / "p"
    projects.mkdir(parents=True)
    (projects / "conv-a3.jsonl").write_text(
        json.dumps({"type": "custom-title", "customTitle": "Fonctionnalités"}) + "\n"
    )
    bridge_env.write_session_file(team.a, "conv-a3")
    team.start(team.a, "conv-a3", "clear")
    bridge_env.stub_log.write_text("")

    bridge_env.cli("sessions")
    asked = team.prompt(team.a, "conv-a3", "next")
    again = team.prompt(team.a, "conv-a3", "next again")

    assert "session rename" not in team.log()
    assert "Give this tab a short English name" in asked
    assert "English name" not in again


ASSISTANT_ROLE_MAX_CHARS = 2000


def test_assistant_role_comes_with_every_start_and_keeps_its_notes(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll("Alex", "old-a")
    assert bridge_env.cli("handle", "aide", pid=team.b).returncode == 0

    running = team.prompt(team.b, "conv-b", "first prompt as the assistant")
    quiet = team.prompt(team.b, "conv-b", "second")
    saved = team.guild("notes", "--write", pid=team.b, stdin="Sam prefers one question at a time.\n")
    compacted = team.start(team.b, "conv-b", "compact")
    resumed = team.start(team.b, "conv-b", "resume")
    cleared = team.start(team.b, "conv-b2", "clear")
    lead_start = team.start(team.a, "conv-a", "resume")

    assert "You are Aide (`aide`), the owner's Right Hand" in running
    assert "team leads (Alex)" in running
    assert "You route and manage, and effectiveness comes first." in running
    assert "Delegate by default to save your context" in running and "nothing bans depth" in running
    assert "self-contained HTML page that you publish" in running and "Spot-check a lead's claim" in running
    assert "offer the owner to talk to the lead in its tab, and tell the lead that the owner decides there" in running
    assert "nobody else" not in running
    assert "[guild]" not in quiet
    assert saved.returncode == 0, saved.stderr
    assert (bridge_env.state_dir / "assistant" / "notes.md").exists()
    for start in (compacted, resumed, cleared):
        assert "Right Hand" in start
        assert "Sam prefers one question at a time." in start
    assert "Right Hand" not in lead_start
    assert len(running) <= ASSISTANT_ROLE_MAX_CHARS


def test_assistant_role_names_the_owner_and_the_words_from_config() -> None:
    default = convert_config_to_assistant_role(map_toml_to_config({}), [])
    config = map_toml_to_config({"owner_name": "Sam", "night_words": ["Good night", "Nite"]})
    named = convert_config_to_assistant_role(config, [])

    assert "the owner's Right Hand; the owner leads" in default
    assert "'Good night' starts the night: run `guild dream --wait-all` (Bash" in default
    assert "the owner its total time in one line." in default
    assert "Sam's Right Hand; Sam leads" in named and "the owner" not in named
    assert "'Good night' or 'Nite' starts the night: run" in named and "tell Sam its total time" in named
    assert len(named) <= ASSISTANT_ROLE_MAX_CHARS


def test_assistant_stays_out_of_lead_paths(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll("Alex", "old-a")
    assert bridge_env.cli("handle", "aide", pid=team.b).returncode == 0

    listed = json.loads(team.guild("leads", "--json").stdout)
    ack = team.guild("ack", "1", "nothing for me", pid=team.b)
    other = team.prompt(team.a, "conv-a", "a lead, not the assistant")
    enrolled = team.enroll("Aide", "aide")

    assert [r["name"] for r in listed] == ["alex"]
    assert ack.returncode == 2
    assert enrolled.returncode != 0
    assert "the assistant's" in enrolled.stderr
    assert "Right Hand" not in other


def test_former_assistant_handles_route_to_the_assistant_and_only_it_relays(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    for handle in ("ward", "helm", "aide"):
        assert bridge_env.cli("handle", handle, pid=team.b).returncode == 0
    bridge_env.cli("post", "decision", "Ship?", pid=team.a)

    by_helm = bridge_env.cli("send", "helm", "former name", pid=team.a)
    by_ward = bridge_env.cli("send", "ward", "first name", pid=team.a)
    answer_refused = team.guild("answer", "1", "yes", pid=team.a)
    priority_refused = team.guild("priority", "set", "export=PROJ-27", pid=team.a)
    answered = team.guild("answer", "1", "yes", pid=team.b)
    prioritised = team.guild("priority", "set", "export=PROJ-27", pid=team.b)

    assert by_helm.returncode == 0, by_helm.stderr
    assert "→ aide" in by_helm.stdout
    assert by_ward.returncode == 0, by_ward.stderr
    assert "→ aide" in by_ward.stdout
    assert answer_refused.returncode == 2
    assert "only aide" in answer_refused.stderr
    assert priority_refused.returncode == 2
    assert answered.returncode == 0, answered.stderr
    assert prioritised.returncode == 0, prioritised.stderr


def test_locked_assistant_gets_no_role(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    assert bridge_env.cli("handle", "aide", pid=team.b).returncode == 0
    team.write_tree(workspace_b="🔒 Misc")
    bridge_env.cli("sessions")

    assert "Right Hand" not in team.start(team.b, "conv-b", "resume")


def test_a_second_pane_in_the_assistants_tab_cannot_become_a_lead(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    helper = spawn_process(env={"AGTERM_PANE_ID": "PANE-C", "AGTERM_SESSION_ID": "TAB-B"})
    bridge_env.write_session_file(helper, "conv-c")
    raw = json.loads(bridge_env.stub_tree.read_text())
    raw["result"]["tree"]["workspaces"][0]["sessions"][1]["surfaces"].append({"paneID": "PANE-C"})
    bridge_env.stub_tree.write_text(json.dumps(raw))
    payload = {"session_id": "conv-c", "cwd": str(bridge_env.root), "source": "startup"}
    bridge_env.hook("SessionStart", payload, helper, {"AGTERM_PANE_ID": "PANE-C"})
    assert bridge_env.cli("handle", "aide", pid=team.b).returncode == 0
    assert bridge_env.cli("handle", "helper", pid=helper).returncode == 0

    enrolled = team.enroll("Alex", "helper")

    assert enrolled.returncode != 0
    assert "the assistant's" in enrolled.stderr


def _start_pending_files(bridge_env: BridgeEnv) -> list[str]:
    directory = bridge_env.state_dir / "start-pending"
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


def test_a_compaction_start_that_cannot_write_still_gives_the_list_role_and_notes_and_is_recorded_after(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.guild("notes", "--write", pid=team.a, stdin="Owns PROJ-33.\n")
    assert "You are Alex, a team lead" in team.prompt(team.a, "conv-a", "first prompt after enrolling")
    assert bridge_env.cli("keep", "PR acme/web#7 waits for review", pid=team.a).returncode == 0
    blocker = bridge_env.connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        compacted = team.start(team.a, "conv-a", "compact")
        still_locked = team.prompt(team.a, "conv-a", "while the lock still holds")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    pending = _start_pending_files(bridge_env)

    following = team.prompt(team.a, "conv-a", "next")
    after_compact = team.start(team.a, "conv-a", "compact")

    assert "database is locked" in (bridge_env.state_dir / "errors.log").read_text()
    assert compacted.index("PR acme/web#7 waits for review") < compacted.index("You are Alex, a team lead")
    assert compacted.index("You are Alex, a team lead") < compacted.index("Owns PROJ-33.")
    assert still_locked == ""
    assert pending == ["conv-a.json"], "a make-up whose own transaction fails puts the start back"
    assert "You are Alex" not in following and "acme/web#7" not in following, "given once, then only recorded"
    assert _start_pending_files(bridge_env) == []
    with bridge_env.connect() as conn:
        row = conn.execute("SELECT compacted_at, start_source FROM conversations WHERE conversation_id = 'conv-a'")
        compacted_at, source = row.fetchone()
    assert compacted_at is not None and source == "compact"
    assert "You are Alex, a team lead" in after_compact, "the role was recorded as seen by this conversation only"


def test_a_start_that_can_neither_write_nor_read_says_where_its_context_is_and_the_next_prompt_brings_it(
    bridge_env: BridgeEnv,
    spawn_process: SpawnProcess,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.guild("notes", "--write", pid=team.a, stdin="Owns PROJ-33.\n")
    team.prompt(team.a, "conv-a", "first prompt after enrolling")
    assert bridge_env.cli("keep", "PR acme/web#7 waits for review", pid=team.a).returncode == 0
    for key, value in {**bridge_env.env, "CLAUDE_PID": str(team.a), "AGTERM_PANE_ID": "PANE-A"}.items():
        monkeypatch.setenv(key, value)
    payload = {"session_id": "conv-a", "cwd": str(bridge_env.root), "source": "compact"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    def unreadable(*_: object) -> list[Part]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hooks, "read_start_parts", unreadable)
    blocker = bridge_env.connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        hooks.run_hook("SessionStart")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    compacted = _context(capsys.readouterr().out)
    monkeypatch.undo()

    following = team.prompt(team.a, "conv-a", "next")

    assert "`bridge keep --show`, `guild notes`" in compacted
    assert following.index("PR acme/web#7 waits for review") < following.index("You are Alex, a team lead")
    assert following.index("You are Alex, a team lead") < following.index("Owns PROJ-33.")
    assert _start_pending_files(bridge_env) == []
