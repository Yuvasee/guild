#!/usr/bin/env python3
"""Refuses a commit or push that carries words, ids or secrets this install keeps private.

    scripts/leak-gate.py --staged            the index, as the next commit would hold it (pre-commit)
    scripts/leak-gate.py --tree DIR          any directory
    scripts/leak-gate.py --pre-push          the pushed commits' trees and metadata, refs on stdin (pre-push)
    scripts/leak-gate.py --commits A..B      those commits' trees and metadata
    scripts/leak-gate.py --commit-msg FILE   a commit message as it is written, before the commit exists (commit-msg)
    add --all to also print the hits allow.txt accepts

The lists never live in this repo. They are read from GUILD_LEAK_DIR, else `git config guild.leakDir`:

    denylist*.txt       regexes, case-insensitive; \\b is a letter boundary (snake_case, digits and \\n separate
                        words); (?-i:...) makes a term case-sensitive; # starts a comment
    denylist-ids.txt    real PR and issue numbers, matched where they read as ids (#N, KEY-N, pr:N, pull/N,
                        issues/N) and, from 4 digits, as bare numbers too; a leading ~ keeps a number to id
                        positions only
    deny-exceptions.txt regexes (case-sensitive) blanked out of a line before the denylists run
    config.toml, people/people.toml
                        every string value of 4+ characters, every integer of 5+ digits and every table key that
                        is not a plain identifier is refused literally; issue_prefixes also refuse KEY-N
    literals*.txt       more literals refused as written, one per line: ids of private pages, sessions, files
    values-ignore.txt   literal values from those files that are generic and may appear
    allow.txt           hits of the built-in patterns judged harmless: `regex` (full match of the hit) or
                        `path-glob<TAB>regex`

Built-in patterns, for any install: emails outside example and noreply domains, 12-digit numbers, ARNs, AWS
resource ids, Slack ids, IPv4 addresses other than 127.0.0.1 and 0.0.0.0, home paths, UUIDs. Images are read by
OCR (tesseract, ffmpeg for animations), cached per blob in the git dir. gitleaks runs over the tree and, for
commits, over their history; a missing gitleaks or list dir fails the gate.
"""

import argparse
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

LETTER = r"[^\W\d_]"
LETTER_BOUNDARY = rf"(?:(?:(?<!{LETTER})|(?<=\\[ntr]))(?={LETTER})|(?<={LETTER})(?!{LETTER}))"
ESCAPE_RE = re.compile(r"\\[ntr]")
ID_CONTEXT = r"(?:#|(?<=[A-Za-z])-|\bpr:|/pull/|/pulls/|/issues/)"
BINARY_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SKIP_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
SHA_RE = re.compile(r"sha(?:256|512):[0-9a-f]{32,}|\b[0-9a-f]{40,}\b")
ZERO = "0" * 40
OK_EMAIL_DOMAINS = re.compile(
    r"(?:^|\.)(?:example\.(?:com|org|net)|example|test|invalid|localhost|users\.noreply\.github\.com|noreply\.github\.com)$",
    re.I,
)
BUILTIN = {
    "email": re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)"),
    "12-digit": re.compile(r"(?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])"),
    "arn": re.compile(r"\barn:aws[\w-]*:\S*", re.I),
    "aws-id": re.compile(r"\b(?:i|vpc|sg|subnet|ami|vol|eni|igw|rtb|snap|nat)-(?:[0-9a-f]{17}|[0-9a-f]{8})\b"),
    "slack-id": re.compile(r"(?<![A-Za-z0-9])[UCTWGD](?=[A-Z]*[0-9])[A-Z0-9]{8,10}(?![A-Za-z0-9])"),
    "ipv4": re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    "home-path": re.compile(r"/(?:Users|home)/[^/\s\"'`)<>:;,]+"),
    "uuid": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
}
OK_IPS = {"127.0.0.1", "0.0.0.0"}


def fail(message: str) -> None:
    print(f"leak-gate: {message}", file=sys.stderr)
    sys.exit(2)


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        fail(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def find_list_dir() -> Path:
    raw = os.environ.get("GUILD_LEAK_DIR") or git("config", "--get", "guild.leakDir", check=False).strip()
    if not raw:
        fail("no list dir: set GUILD_LEAK_DIR or `git config guild.leakDir <dir>`")
    path = Path(raw).expanduser()
    if not path.is_dir():
        fail(f"list dir {path} does not exist")
    return path


def read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line.rstrip("\n") for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


def collect_values(node: object, out: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", key):
                out.add(key)
            collect_values(value, out)
    elif isinstance(node, list):
        for value in node:
            collect_values(value, out)
    elif isinstance(node, bool):
        return
    elif isinstance(node, int) and len(str(abs(node))) >= 5:
        out.add(str(node))
    elif isinstance(node, str) and len(node) >= 4:
        out.add(node)


class Lists:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.terms: list[re.Pattern[str]] = []
        for path in sorted(root.glob("denylist*.txt")):
            if path.name != "denylist-ids.txt":
                self.terms += [re.compile(p.replace("\\b", LETTER_BOUNDARY), re.I) for p in read_lines(path)]
        self.exceptions = [re.compile(p) for p in read_lines(root / "deny-exceptions.txt")]
        ids = read_lines(root / "denylist-ids.txt")
        context_ids = sorted({i.lstrip("~").strip() for i in ids}, key=len, reverse=True)
        bare = {i.strip() for i in ids if not i.startswith("~") and len(i.strip()) >= 4}
        bare_ids = sorted(bare, key=len, reverse=True)
        if context_ids:
            self.terms.append(re.compile(rf"{ID_CONTEXT}(?:{'|'.join(context_ids)})(?![0-9A-Za-z])", re.I))
        if bare_ids:
            # A letter may follow (a test named like_1234s), not precede (hex, base62 ids).
            self.terms.append(re.compile(rf"(?<![0-9A-Za-z.])(?:{'|'.join(bare_ids)})(?![0-9])"))
        values: set[str] = set()
        prefixes: list[str] = []
        for name in ("config.toml", "people/people.toml"):
            path = root / name
            if path.is_file():
                data = tomllib.loads(path.read_text())
                collect_values(data, values)
                prefixes += [str(p) for p in data.get("issue_prefixes", [])]
        for path in sorted(root.glob("literals*.txt")):
            values.update(line.strip() for line in read_lines(path) if len(line.strip()) >= 4)
        values -= set(read_lines(root / "values-ignore.txt"))
        self.values = sorted(values, key=len, reverse=True)
        if prefixes:
            self.terms.append(re.compile(rf"\b(?:{'|'.join(map(re.escape, prefixes))})-[0-9]+", re.I))
        self.allow: list[tuple[str, re.Pattern[str]]] = []
        for line in read_lines(root / "allow.txt"):
            glob, _, regex = line.rpartition("\t")
            self.allow.append((glob or "*", re.compile(regex)))
        if not self.terms and not self.values:
            fail(f"{root} holds no denylist and no config values")

    def private_hits(self, line: str) -> list[str]:
        line = SHA_RE.sub(" ", line)
        for exc in self.exceptions:
            line = exc.sub(" ", line)
        found = []
        for variant in dict.fromkeys((line, ESCAPE_RE.sub("  ", line))):
            found += [m.group(0) for p in self.terms for m in p.finditer(variant)]
        found += [v for v in self.values if v in line]
        return list(dict.fromkeys(found))

    def builtin_hits(self, line: str) -> list[tuple[str, str]]:
        line = SHA_RE.sub(" ", line)
        found = []
        for kind, pattern in BUILTIN.items():
            for m in pattern.finditer(line):
                if kind == "email" and OK_EMAIL_DOMAINS.search(m.group(1)):
                    continue
                if kind == "ipv4" and (m.group(0) in OK_IPS or any(int(o) > 255 for o in m.group(0).split("."))):
                    continue
                found.append((kind, m.group(0)))
        return found

    def allowed(self, rel: str, hit: str) -> bool:
        return any(fnmatch.fnmatch(rel, glob) and regex.fullmatch(hit) for glob, regex in self.allow)


class Report:
    def __init__(self, show_all: bool) -> None:
        self.show_all = show_all
        self.problems = 0
        self.accepted = 0

    def private(self, where: str, hit: str) -> None:
        self.problems += 1
        print(f"PRIVATE  {where}: {hit!r}")

    def builtin(self, lists: Lists, rel: str, where: str, kind: str, hit: str) -> None:
        if lists.allowed(rel, hit):
            self.accepted += 1
            if self.show_all:
                print(f"allowed  {where}: {kind} {hit!r}")
            return
        self.problems += 1
        print(f"PATTERN  {where}: {kind} {hit!r}")

    def check_text(self, lists: Lists, rel: str, text: str, label: str = "") -> None:
        for lineno, line in enumerate(text.splitlines(), 1):
            where = f"{rel}{label}:{lineno}"
            for hit in lists.private_hits(line):
                self.private(where, hit)
            for kind, hit in lists.builtin_hits(line):
                self.builtin(lists, rel, where, kind, hit)


def ocr(path: Path, cache: Path | None) -> str:
    key = hashlib.sha256(path.read_bytes()).hexdigest()
    cached = cache / f"{key}.txt" if cache else None
    if cached and cached.is_file():
        return cached.read_text()
    tesseract = shutil.which("tesseract")
    if not tesseract:
        fail("tesseract missing, images cannot be read: brew install tesseract")
    texts = []
    with tempfile.TemporaryDirectory(prefix="leak-gate-frames-") as raw:
        frames = [path]
        if path.suffix.lower() in (".gif", ".webp"):
            ffmpeg = shutil.which("ffmpeg") or fail("ffmpeg missing, animations cannot be read: brew install ffmpeg")
            subprocess.run([str(ffmpeg), "-loglevel", "error", "-i", str(path), "-fps_mode", "passthrough",
                            f"{raw}/frame-%04d.png"], check=True)  # fmt: skip
            frames = sorted(Path(raw).glob("frame-*.png"))
        for frame in frames:
            done = subprocess.run([tesseract, str(frame), "stdout"], capture_output=True, text=True, check=False)
            if done.returncode != 0:
                fail(f"OCR of {path.name} failed: {done.stderr.strip()[-200:]}")
            texts.append(done.stdout)
    text = "\n".join(texts)
    if cached:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(text)
    return text


def check_tree(lists: Lists, tree: Path, report: Report, cache: Path | None, label: str = "") -> None:
    for path in sorted(tree.rglob("*")):
        rel_path = path.relative_to(tree)
        if any(part in SKIP_PARTS for part in rel_path.parts) or not path.is_file():
            continue
        rel = str(rel_path)
        for hit in lists.private_hits(rel):
            report.private(f"{label}name {rel}", hit)
        if path.suffix.lower() in BINARY_IMAGES:
            report.check_text(lists, rel, ocr(path, cache), f"{label} (image text)")
            continue
        data = path.read_bytes()
        try:
            text = data.decode()
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        report.check_text(lists, rel, text, label)
    run_gitleaks(["dir", str(tree)], f"tree{label}", report)


def run_gitleaks(args: list[str], label: str, report: Report) -> None:
    gitleaks = shutil.which("gitleaks")
    if not gitleaks:
        fail("gitleaks missing: brew install gitleaks")
    done = subprocess.run([gitleaks, *args, "--no-banner", "--redact", "--exit-code", "1"],
                          capture_output=True, text=True, check=False)  # fmt: skip
    if done.returncode != 0:
        report.problems += 1
        print(f"GITLEAKS {label}:\n{(done.stdout + done.stderr).strip()}")


def check_commits(lists: Lists, repo: Path, revs: list[str], report: Report, cache: Path) -> None:
    commits = git("rev-list", *revs, cwd=repo).split()
    if not commits:
        return
    for sha in commits:
        meta = git("log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%ai%n%ci%n%B", sha, cwd=repo)
        lines = meta.splitlines()
        report.check_text(lists, "commit", meta, f" {sha[:12]}")
        for ident in lines[:2]:
            email = ident.rpartition("<")[2].rstrip(">")
            if not OK_EMAIL_DOMAINS.search(email.rpartition("@")[2]):
                report.private(f"commit {sha[:12]} identity", ident)
        for stamp in lines[2:4]:
            if not stamp.endswith("+0000"):
                report.private(f"commit {sha[:12]} date not in UTC", stamp)
    with tempfile.TemporaryDirectory(prefix="leak-gate-tree-") as raw:
        tip = commits[0]
        archive = subprocess.run(["git", "archive", tip], cwd=repo, capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", raw], input=archive, check=True)
        check_tree(lists, Path(raw), report, cache, f" @{tip[:12]}")
    run_gitleaks(["git", str(repo), f"--log-opts={' '.join(revs)}"], "history", report)


def read_push_revs(repo: Path) -> list[list[str]]:
    ranges = []
    for line in sys.stdin.read().splitlines():
        _local_ref, local_sha, _remote_ref, remote_sha = line.split()
        if local_sha == ZERO:
            continue
        # A remote tip this clone never fetched cannot bound the range: every commit not on a remote is checked.
        known = remote_sha != ZERO and subprocess.run(["git", "cat-file", "-e", remote_sha], cwd=repo).returncode == 0
        ranges.append([local_sha, f"^{remote_sha}"] if known else [local_sha, "--not", "--remotes"])
    return ranges


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--tree", type=Path)
    mode.add_argument("--pre-push", action="store_true")
    mode.add_argument("--commits")
    mode.add_argument("--commit-msg", type=Path)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    lists = Lists(find_list_dir())
    report = Report(args.all)
    repo = Path(git("rev-parse", "--show-toplevel", check=False).strip() or ".")
    git_dir = git("rev-parse", "--absolute-git-dir", check=False).strip()
    cache = Path(git_dir) / "leak-gate-ocr" if git_dir else None
    if args.commit_msg:
        # Lines git strips before it stores the message (status, paths) are not the message.
        lines = [line for line in args.commit_msg.read_text().splitlines() if not line.startswith("#")]
        report.check_text(lists, "commit message", "\n".join(lines))
    elif args.tree:
        check_tree(lists, args.tree.resolve(), report, cache)
    elif args.staged:
        with tempfile.TemporaryDirectory(prefix="leak-gate-index-") as raw:
            git("checkout-index", "--all", f"--prefix={raw}/", cwd=repo)
            check_tree(lists, Path(raw), report, cache, " (staged)")
    else:
        assert cache is not None
        for revs in read_push_revs(repo) if args.pre_push else [[args.commits]]:
            check_commits(lists, repo, revs, report, cache)
    print(f"leak-gate: {report.problems} problem(s), {report.accepted} accepted by allow.txt "
          f"({len(lists.terms)} denylist patterns, {len(lists.values)} config values from {lists.root})")  # fmt: skip
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
