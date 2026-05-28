# oh-my-skill

A browser-based manager for **markdown skill cards** — short, scannable
reference cards (aliases, env vars, recipes, keybindings) with:

- ✨ **AI Extract** — paste rough notes, get a tight skill card back
- 💬 **Per-card Chat** — chat with Claude or Codex scoped to a single card
- 🔄 **GitHub Sync** — diff-based push/pull/delete with public + private repos
- 🏷 **Tag-based routing** — `private`-tagged cards go to your private repo
- 🙈 **Ignore patterns** — keep `sibyl-*` (or anything else) out of the diff

## Install

Requires **Python 3.11+** and `git`. Optional: [`claude`](https://docs.claude.com/en/docs/claude-code) and/or [`codex`](https://developers.openai.com/codex) on `PATH` for AI features.

```bash
# Recommended: pipx (isolated, like npx for Python)
pipx install git+https://github.com/ByteDennis/oh-my-skill.git

# Or, plain pip
pip install --user git+https://github.com/ByteDennis/oh-my-skill.git
```

Then:

```bash
oh-my-skill                    # opens http://localhost:5009 in your browser
oh-my-skill --port 8080
oh-my-skill --no-browser
oh-my-skill --data-dir ~/foo   # custom storage location
oh-my-skill --version
```

By default, data lives in `~/.local/share/oh-my-skill/` (`$XDG_DATA_HOME/oh-my-skill` if set).

## Update

```bash
pipx upgrade oh-my-skill
```

(`pipx` will re-fetch from GitHub `main`. Pin a version with
`pipx install "git+https://github.com/ByteDennis/oh-my-skill.git@v0.2.0"`.)

## Optional: AI features

Extract uses `claude -p`. Per-card chat can use either `claude -p` or
`codex exec --json`, depending on **Settings → Chat provider**. If the
selected provider is unavailable, the chat UI degrades gracefully and the
rest of the app keeps working.

To enable:

1. Install [Claude Code](https://docs.claude.com/en/docs/claude-code):
   `npm i -g @anthropic-ai/claude-code`
2. Optional for chat: install Codex CLI:
   `npm i -g @openai/codex`
3. For Claude, open **Settings → Claude OAuth token** in oh-my-skill, paste
   your token (or set `CLAUDE_CODE_OAUTH_TOKEN` in the environment).
4. For Codex, run `codex login` first. The app checks `codex login status`
   before enabling Codex chat.

## Optional: GitHub sync

Open **Settings → GitHub sync** and fill in:
- **Public repo** URL — receives all cards *without* the `private` tag
- **Private repo** URL — receives all cards *with* the `private` tag
- **Branch** (default `main`), **Subdir** (optional), **SSH key path**
- **Ignore patterns** — globs like `sibyl-*`, one per line

Then click **🔄 Sync** in the toolbar. The Sync Center shows a per-card
diff (`local-only` / `remote-only` / `modified` / `synced`); each row has
a dropdown to push/pull/delete-remote/delete-local/skip. Conflict
resolution: newer `updated_at` wins (you can override).

## Docker

```bash
docker compose up -d oh-my-skill
```

The Docker image bundles `tmux`/`ttyd`/the `claude` binary so AI features
work out of the box.

## License

MIT
