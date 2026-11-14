# For coding agents

- Asked to install or set up Guild for the user: follow [SETUP.md](SETUP.md) step by step.
- Working on Guild's code: Python 3.12, `uv sync`, then `uv run pytest` and `uv run ruff check`.
  Never run `bridge install-hooks`, `install-service` or `uv tool install` from a development checkout on a machine
  where Guild is already in use: they replace the user's live setup. Run tests and `bridge serve --port <spare>` with
  `HOME`, `BRIDGE_STATE_DIR` and `BRIDGE_CONFIG` pointing at a temp dir.
- Writing an adapter (task tracker, chat, code host): [docs/adapters.md](docs/adapters.md).
- `scripts/leak-gate.py` refuses a commit or push that names what an install keeps private; its lists live outside
  the repo (`GUILD_LEAK_DIR` or `git config guild.leakDir`). Install it as `pre-commit` (`--staged`) and `pre-push`
  (`--pre-push`) hooks in a clone that must not leak.
