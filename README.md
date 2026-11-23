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
2. Press **Copy for Claude** and paste it to your assistant (Claude Code, Codex, any agent with a shell), with what you want researched. It sets the goal.
3. Press **Start loop**, then read the page. Nothing waits for you: each round takes the best place to look, and every result is attacked by a fresh round before it counts.
4. To steer it or have it explained, ask your assistant. It reads `http://127.0.0.1:8765/api/status` and acts through the API.

## What you get

- **A page that only shows.** Overview (is it running, what is it doing, what counts, what it cost), Research (every claim with each gate's verdict), Live (the agent's output as it happens), Write-ups.
- **Gates.** A result counts only after tests the round does not control: a command (a replay under a sanitizer, a proof that builds), a fresh round told to refute it, or both. A project with none gets a built-in refuter. Ask your assistant to write stronger ones.
- **Write-ups.** Ask your assistant how something works and it saves the explanation as a Markdown file you can read on the page and share.
- **Side-by-side rounds and a spend limit.** Up to four loops at once, each on its own place to look, with one cap on the total model rounds a launch may use.
- **Any agent.** Claude Code, Codex and OhMyPi have each run rounds; a recipe is one command in `launchers.json`.

It runs the same way on Windows, macOS and Linux. Your project records stay on your machine.

[Guide](docs/guide.md) · [For the assistant](AGENTS.md)
