<picture><source media="(prefers-color-scheme: dark)" srcset="logo-dark.png"><img src="logo.png" width="72" alt="Hades logo"></picture>

# Hades

Hades runs an agent (Claude Code, Codex, any command) round after round on a research question. Each result has to pass tests the round that made it does not control before it counts. You start it and read the page; an assistant reads the same facts as text and steers it for you.

## Run locally

You need Python 3 and an agent to run: [Claude Code](https://docs.anthropic.com/en/docs/claude-code), Codex or another command-line agent. Nothing else to install.

```sh
git clone https://github.com/0xp5k/hades
cd hades
```

| Platform | Start Hades |
| --- | --- |
| Windows | `py -3 scripts\hades.py` |
| Linux or macOS | `python3 scripts/hades.py` |

1. Answer yes when it asks to allow the Start button. The page then opens.
2. Type what to research, then press **Start**.
3. Read the page. Nothing waits for you: each round takes the best place to look, every result is attacked by a fresh round before it counts, and the page shows what survived and what it cost.
4. To steer it or have it explained, open your assistant and say: `Read http://127.0.0.1:8765/api/status and help me with Hades.`

For stronger checking than the built-in refuter, give a project gates of its own (a replay under a sanitizer, a proof that builds) or ask your assistant to write them. It runs the same way on Windows, macOS and Linux.

Your project records stay on your machine.

[Guide](docs/guide.md) · [For the assistant](AGENTS.md)
