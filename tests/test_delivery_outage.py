import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from bridge import tabinput
from bridge.config import Config
from bridge.deploycheck import CAT_READY, _shows_answer, check_typing  # pyright: ignore[reportPrivateUsage]
from bridge.errlog import log_slow
from bridge.outage import AGTERM_SILENT_NOTICE_AFTER_MS, OutageNotices
from bridge.wake import WakeWatch

NOW = 10_000_000


def test_one_tab_cut_off_is_for_the_assistant_alone() -> None:
    assert OutageNotices().take(["denis"], "xavi", None, NOW) == []


def test_two_tabs_cut_off_reach_the_owner_once_per_episode() -> None:
    notices = OutageNotices()

    first = notices.take(["denis", "alex"], "xavi", None, NOW)
    again = notices.take(["denis", "alex", "charlie"], "xavi", None, NOW + 30_000)
    cleared = notices.take([], "xavi", None, NOW + 60_000)
    next_episode = notices.take(["boris", "eli"], "xavi", None, NOW + 90_000)

    assert first == [("tabs", "bridge: messages are not reaching alex, denis")]
    assert again == [] and cleared == []
    assert next_episode == [("tabs", "bridge: messages are not reaching boris, eli")]


def test_the_assistants_own_tab_cut_off_reaches_the_owner() -> None:
    assert OutageNotices().take(["xavi"], "xavi", None, NOW) == [("tabs", "bridge: messages are not reaching xavi")]


def test_agterm_silent_past_the_limit_reaches_the_owner() -> None:
    notices = OutageNotices()
    since = NOW - AGTERM_SILENT_NOTICE_AFTER_MS

    early = notices.take([], "xavi", NOW - 60_000, NOW)
    late = notices.take([], "xavi", since, NOW)
    still = notices.take([], "xavi", since, NOW + 30_000)

    assert early == [] and still == []
    assert late == [("agterm", "bridge: agterm has not answered for 5 min")]


def test_a_notice_that_did_not_show_is_tried_again() -> None:
    notices = OutageNotices()
    first = notices.take(["denis", "alex"], "xavi", None, NOW)
    notices.release("tabs")

    assert notices.take(["denis", "alex"], "xavi", None, NOW + 30_000) == first


def test_hours_asleep_are_not_agterm_silence() -> None:
    clock = {"wall": 1_000.0, "mono": 1_000.0}
    watch = WakeWatch(lambda: clock["wall"], lambda: clock["mono"])
    watch.note_agterm(False)
    clock["wall"] += 8 * 3600 + 1
    clock["mono"] += 1

    assert watch.silent_since() == int(clock["wall"] * 1000)


def test_a_hung_agterm_is_logged_once_per_interval_with_a_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tabinput, "_agterm_trouble", tabinput._AgtermTrouble())  # pyright: ignore[reportPrivateUsage]

    def hang(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired("agtermctl", 10)

    with mock.patch("bridge.tabinput.subprocess.run", hang):
        for _ in range(3):
            assert tabinput.run_agterm(Config(), ["session", "text", "--target", "S"]) is None

    log = (tmp_path / "errors.log").read_text()
    assert log.count("agtermctl session text: timed out") == 1


def test_an_answering_agterm_leaves_no_log_line_even_on_a_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tabinput, "_agterm_trouble", tabinput._AgtermTrouble())  # pyright: ignore[reportPrivateUsage]
    done = subprocess.CompletedProcess(["agtermctl"], 1, stdout="", stderr="no such session")

    with mock.patch("bridge.tabinput.subprocess.run", return_value=done):
        assert tabinput.run_agterm(Config(), ["session", "text"]) is None

    assert not (tmp_path / "errors.log").exists()


def test_a_slow_agterm_answer_goes_to_the_slow_log_not_the_error_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tabinput, "_agterm_slow", tabinput._AgtermTrouble(log_slow))  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(tabinput, "AGTERM_SLOW_S", 0.0)
    done = subprocess.CompletedProcess(["agtermctl"], 0, stdout="tree", stderr="")

    with mock.patch("bridge.tabinput.subprocess.run", return_value=done):
        assert tabinput.run_agterm(Config(), ["tree", "--json"]) == "tree"

    assert "agtermctl tree --json: slow" in (tmp_path / "slow.log").read_text()
    assert not (tmp_path / "errors.log").exists()


class FakeAgterm:
    """A tab whose command announces cat and runs it once its shell has started: what is typed echoes at once, and an
    Enter makes cat print the line again, when cat runs."""

    def __init__(
        self,
        enter_takes: bool = True,
        reply_lost: bool = False,
        close_fails: bool = False,
        cat_starts_on_read: int = 1,
    ) -> None:
        self.enter_takes = enter_takes
        self.reply_lost = reply_lost
        self.close_fails = close_fails
        self.cat_starts_on_read = cat_starts_on_read
        self.announces = False
        self.cat_runs = False
        self.reads = 0
        self.name = ""
        self.screen = ""
        self.line = ""
        self.unread: list[str] = []
        self.typed: list[str] = []
        self.closed: list[str] = []

    def __call__(self, config: Config, args: list[str]) -> str | None:
        if args[:1] == ["tree"]:
            sessions = [{"id": "CHECK", "name": self.name}] if self.name else []
            return json.dumps({"result": {"tree": {"workspaces": [{"sessions": sessions}]}}})
        if args[:2] == ["session", "new"]:
            self.name = args[args.index("--name") + 1]
            self.announces = CAT_READY in args[args.index("--command") + 1]
            return None if self.reply_lost else json.dumps({"result": {"id": "CHECK"}})
        if args[:2] == ["session", "type"]:
            text = args[-1]
            self.typed.append(text)
            if text == tabinput.ENTER:
                if self.enter_takes:
                    self.screen += f"{self.line}\n"
                    self.unread.append(self.line)
                    self.line = ""
            else:
                self.line += text
            self._let_cat_read()
            return ""
        if args[:2] == ["session", "text"]:
            self.reads += 1
            if not self.cat_runs and self.reads >= self.cat_starts_on_read:
                self.cat_runs = True
                self.screen += f"{CAT_READY}\n" if self.announces else ""
            self._let_cat_read()
            return self.screen + self.line
        if args[:2] == ["session", "close"]:
            if self.close_fails:
                return None
            self.closed.append(args[-1])
            return ""
        return None

    def _let_cat_read(self) -> None:
        if self.cat_runs:
            self.screen += "".join(f"{line}\n" for line in self.unread)
            self.unread = []


def test_the_deploy_check_passes_when_text_and_enter_take(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAgterm()
    monkeypatch.setattr("bridge.deploycheck.run_agterm", fake)

    result = check_typing(Config())

    assert result.ok, result.detail
    assert fake.closed == ["CHECK"]


def test_the_deploy_check_fails_when_the_enter_does_not_take(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAgterm(enter_takes=False)
    monkeypatch.setattr("bridge.deploycheck.run_agterm", fake)
    monkeypatch.setattr("bridge.deploycheck.LANDS_WITHIN_S", 0.5)

    result = check_typing(Config())

    assert not result.ok
    assert "the text but not its Enter" in result.detail
    assert fake.closed == ["CHECK"]


def test_the_deploy_check_times_the_typing_from_when_cat_runs_not_from_when_its_tab_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAgterm(cat_starts_on_read=3)
    monkeypatch.setattr("bridge.deploycheck.run_agterm", fake)
    monkeypatch.setattr("bridge.deploycheck.LANDS_WITHIN_S", 0.2)

    result = check_typing(Config())

    assert result.ok, result.detail
    assert "cat started in" in result.detail


def test_the_deploy_check_types_nothing_into_a_tab_whose_cat_never_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAgterm(cat_starts_on_read=10**9)
    monkeypatch.setattr("bridge.deploycheck.run_agterm", fake)
    monkeypatch.setattr("bridge.deploycheck.CAT_STARTS_WITHIN_S", 0.2)

    result = check_typing(Config())

    assert not result.ok
    assert "did not start cat" in result.detail
    assert fake.typed == []
    assert fake.closed == ["CHECK"]


def test_the_deploy_check_fails_when_agterm_does_not_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def silent(config: Config, args: list[str]) -> str | None:
        return None

    monkeypatch.setattr("bridge.deploycheck.run_agterm", silent)

    result = check_typing(Config())

    assert not result.ok and "does not answer" in result.detail


def test_a_check_tab_whose_reply_was_lost_is_found_by_name_and_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAgterm(reply_lost=True)
    monkeypatch.setattr("bridge.deploycheck.run_agterm", fake)

    result = check_typing(Config())

    assert result.ok, result.detail
    assert fake.closed == ["CHECK"]


def test_a_check_tab_that_did_not_close_fails_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bridge.deploycheck.run_agterm", FakeAgterm(close_fails=True))

    result = check_typing(Config())

    assert not result.ok and "did not close" in result.detail


def test_the_rewake_check_reads_its_word_answered_in_either_case_but_not_inside_other_text() -> None:
    assert _shows_answer("⏺ kbwkwwkabrmw\n✻ Cogitated for 2s", "kbwkwwkabrmw")
    assert not _shows_answer("> reply with kbwkwwkabrmw please", "kbwkwwkabrmw")
