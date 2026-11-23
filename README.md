<picture><source media="(prefers-color-scheme: dark)" srcset="logo-dark.png"><img src="logo.png" width="72" alt="Hades logo"></picture>

# Hades

Hades runs Claude round after round on a research question and shows what it found, what it ruled out and what is still open. You steer it from the page while it works.

## Run locally

You need Python 3 and, to start agents, [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Nothing else to install.

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
3. Watch it work. Nothing waits for you: each round takes the best place to look, every result is attacked by a fresh round before it counts, and the page ranks what survived.
4. To steer it or have it explained, open Claude and say: `Read http://127.0.0.1:8765/api/status and follow http://127.0.0.1:8765/AGENTS.md to help me with Hades.`

For stronger checking than the built-in refuter, give a project gates of its own (a replay under a sanitizer, a proof that builds) or ask Claude to write them. Rounds have no time limit. It runs the same way on Windows, macOS and Linux.

Your project records stay on your machine.

[Full guide](docs/guide.md) · [Agent reporting instructions](AGENTS.md)
