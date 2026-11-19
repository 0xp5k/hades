<picture><source media="(prefers-color-scheme: dark)" srcset="logo-dark.svg"><img src="logo.svg" width="64" alt="Hades logo"></picture>

# Hades

Hades is a live project page for work you do with Claude or Codex. Give the agent direction in chat. Open the page to see what it found, what’s still open, and the evidence behind an update.

## Run locally

Clone the repo and open its folder. The dashboard uses Python 3 and its standard library.

```sh
git clone https://github.com/0xp5k/hades
cd hades
```

| Platform | Start the dashboard |
| --- | --- |
| Windows | `py -3 server.py --dashboard` |
| Linux or macOS | `python3 server.py --dashboard` |

Open [Hades on localhost](http://127.0.0.1:8765/). The page refreshes as your agent saves updates. Your project records stay on your machine.

[Full guide](docs/guide.md) · [Agent reporting instructions](AGENTS.md)
