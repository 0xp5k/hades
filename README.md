<picture><source media="(prefers-color-scheme: dark)" srcset="logo-dark.png"><img src="logo.png" width="72" alt="Hades logo"></picture>

# Hades

Hades runs an agent (Claude Code, Codex, any command) round after round on a research question. Each result has to pass tests the round that made it does not control before it counts. You start it and read the page; an assistant reads the same facts as text and steers it for you.

## Why

A loop that keeps an agent working is easy to build, and everyone has one. What it produces is the problem: far more claims than anyone can check.

- **DeepMind's Aletheia** ran on 700 open problems and flagged 212 answers as possibly correct. A team of mathematicians graded 200 of them over about three weeks: 137 were fundamentally flawed, and 13 were meaningfully correct ([2602.10177](https://arxiv.org/abs/2602.10177)).
- **DeepMind's AlphaProof Nexus** used the same kind of simple loop with a proof checker behind it. A result that reached a person had already been verified, so the person only had to confirm that the statement was the right one. The paper's conclusion: "Formal verification can serve as a filter for determining which proofs merit human review" ([2605.22763](https://arxiv.org/abs/2605.22763)).

The loop was the same in both. The verifier is what turned weeks of grading into a short list to sign off.

Hades is that pairing for any question you can write a test for: a simple loop, and gates on top of it. The gate is the backboard. The model takes the shot, and the gate sends back an answer it did not write itself, which goes into the next round's brief. It has the shape of a reinforcement-learning loop, but nothing is trained: the model stays as it is, and the place it works gets better. The round decides where to look and how. Hades only keeps the record and decides what counts.

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
3. Press **Start loop**, then read the page. Nothing waits for you: each round goes where it thinks the most is to be found, and every result has to pass the project's gates before it counts.
4. To steer it or have it explained, ask your assistant. It reads `http://127.0.0.1:8765/api/status` and acts through the API.

## What you get

- **A page that only shows.** Overview (is it running, what is it doing, what counts, what it cost), Research (every claim with each gate's verdict), Live (the agent's output as it happens), Write-ups.
- **Gates.** A result counts only after tests the round does not control: a command (a replay under a sanitizer, a proof that builds), a fresh round told to refute it, or both. A project with none gets a built-in refuter. Ask your assistant to write stronger ones.
- **Write-ups.** Ask your assistant how something works and it saves the explanation as a Markdown file you can read on the page and share.
- **Extra checks.** A further verifier that is not required: it runs only on what is already established and shows which results also pass it.
- **Side-by-side rounds and a spend limit.** Up to four loops at once, each on its own place to look. Add or stop one while the others keep running, under one cap on the total model rounds a launch may use.
- **Any agent.** Claude Code, Codex and OhMyPi have each run rounds; a recipe is one command in `launchers.json`.

It runs the same way on Windows, macOS and Linux. Your project records stay on your machine.

[Guide](docs/guide.md) · [For the assistant](AGENTS.md)
