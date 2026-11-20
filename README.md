<picture><source media="(prefers-color-scheme: dark)" srcset="logo-dark.svg"><img src="logo.svg" width="64" alt="Hades logo"></picture>

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
3. Watch Claude work in **Live loop**. Type there to steer it, and read what it found on the project brief.

Rounds have no time limit. In the loop settings you can pick the model, choose **explore** to let Claude follow its nose, and run up to five workers at once. It runs the same way on Windows, macOS and Linux.

Your project records stay on your machine.

[Full guide](docs/guide.md) · [Agent reporting instructions](AGENTS.md)
