<picture><source media="(prefers-color-scheme: dark)" srcset="logo-dark.svg"><img src="logo.svg" width="64" alt="Hades logo"></picture>

# Hades

A live page for long-running AI agent work, and the one port every agent reports to.

Agents (any harness, on any machine) report what they are doing, what they tried and what they found. A separate
verifier re-checks every result, so the page shows what was confirmed, not what was claimed. You read, steer and
approve from the browser.

It needs only Python 3. There is nothing to install.

## Start it

    git clone https://github.com/0xp5k/hades && cd hades
    python3 hades.py

That one command checks the install (ten checks, about five seconds, no model needed), starts the page and the verifier
together, sets up a practice project, and opens http://127.0.0.1:8765.

Or have an assistant do it. Open Claude Code, or any coding agent, in this folder and say:

    Get Hades running for me. Follow "If you are an assistant" in README.md.

Other ways in:

    python3 hades.py check   # only the check; exits 0 if everything works
    python3 demo.py          # three demo projects with simulated agents, to look around without any model
    python3 server.py        # the page on its own, when the verifier runs somewhere else

## Try it on the practice project

`python3 hades.py` creates a project called `practice`: a small interpreter with 17 planted defects and a hidden answer
key, so you can put real agents on it and count what they find. Start an agent whichever way you like:

    sh run-live.sh practice 3        # in another terminal: one Claude Code agent, three rounds

- **From the page:** the gear, then Start agents. This is off until you allow it (`python3 hades.py --allow-launch`).
- **By talking to Claude:** see "Run it by talking to an agent" below.

Watch the page while it works: results arrive on Runs, the verifier confirms or rejects each one, and the map of the
machine fills in from what the verifier measured. Then compare against the answer key:

    python3 toys/tinyvm/answer_key.py score practice

In our own runs Claude Code agents found all 17 in under six minutes. They have the source, so this is the easy one.

Two harder targets are included:

    python3 hades.py new stdlib      # 25 real parsers from Python's standard library; no answer key

- **`stdlib`** is a real target: property lists, tar and zip archives, email, time zone files, TOML and twenty more. A
  defect is an input that makes a parser fail with an error it should not raise for bad input. Nobody knows how many
  there are. Plain random input finds 3 in 2.6 million tries, which is the bar for agents to beat. In our own run,
  three Claude Code (Sonnet) agents confirmed 24 in three short rounds, about five minutes, with no repeats between them.
- **`toys/blackbox`** has no source to read, only a service to send programs to, and 24 planted defects.

The answer key is in this same folder. The brief tells agents to stay in their working folder, but nothing enforces
that. For a measurement you care about, move `toys/*/answer_key.py` off the machine first.

## Start a real project

1. Click **+ Project**, name it, and describe the target in your own words.
2. Start a survey agent with the command the page shows. It studies the target, writes the Understand page so you can
   learn the target too, and proposes a goal, a map of what to cover, and a scoring script.
3. Read the proposal, including the whole scoring script, then approve it or send it back with comments.
4. Start agents. Each round they get a fresh brief: the routine to follow plus where things stand right now.

    python3 loop.py my-project --agent a-1 -- claude -p   # everything after -- is your own harness command

`python3 hades.py` already runs the verifier. If you started `server.py` on its own, run `python3 verifier.py` too, and
keep it where agents cannot write.

`loop.py` works with any harness that takes a prompt on standard input or as a file (`{brief_file}`).
`run-live.sh` is a ready-made wrapper for Claude Code; `check-harness.sh` tries Codex or OhMyPi for one round.

## Run it by talking to an agent

Nothing to install. Open Claude Code on the same machine and say:

    Read http://127.0.0.1:8765/api/coordinator and act as my coordinator for Hades.

It reads a short guide from the server and can then create projects, start and stop agents, queue tasks, change run
settings and tell you what needs you. To have the same abilities built in as tools instead:

    claude mcp add --transport http hades http://127.0.0.1:8765/mcp

The coordinator cannot approve a setup proposal or answer agents' questions for you. Those stay with you, on the page.

## If you are an assistant

A person has asked you to get Hades running on this machine. Do this, in order:

1. Run `python3 hades.py check`. If any line says FAIL, stop and show the person that output.
2. Ask the person one question: may the page, and you, start agents on this machine? Saying yes lets the server run the
   agent command in `launchers.example.json`, and nothing else.
3. Start the server in the background, because it does not exit: `python3 hades.py --skip-check --no-open`, adding
   `--allow-launch` only if they said yes.
4. Read `http://127.0.0.1:8765/api/coordinator` and follow it. It lists your tools and the rules.
5. Tell the person to open http://127.0.0.1:8765, and offer to start one agent on the `practice` project. Starting
   agents spends their model usage, so say how many agents and rounds, and wait for a yes.

## Starting agents from the server

Off by default. `python3 hades.py --allow-launch` switches it on for Claude Code; or copy `launchers.example.json` to
`launchers.json` yourself and edit the command for your harness. Each entry is a
named recipe; Settings and the coordinator can start and stop those recipes and nothing else. Launched agents work in
`agent-work/<project>/<agent>/`, where `loop.log` holds each one's output. A project can name a folder (`agent_files`)
that every new agent starts with a copy of.

## What a harness can call

| Call | What it does |
|---|---|
| `GET  /api/brief?project=X&agent=NAME` | Plain-text instructions plus current state |
| `GET  /api/coordinator` | Plain-text guide for a chat agent that will run the server for you |
| `POST /api/coordinate` | The coordinator's tools, without MCP |
| `POST /api/launch`, `/api/stop` | Start and stop agents from a launch recipe |
| `POST /api/events` | Report anything: activity, run, finding, question, item, task, tool, topic, proposal |
| `POST /api/claim` | Take the oldest queued task |
| `POST /api/slot` | Ask to start a round; honours the project's limit on agents at once |
| `GET  /api/found?project=X` | What is confirmed and what is awaiting the verifier |
| `GET  /api/runs?project=X&agent=NAME` | That agent's results with the verifier's decision |
| `POST /mcp` | The same as MCP tools |

The Connect page in the app has copy-and-paste examples filled in with your server's address.

## On the page

- **Overview**: what needs you, who is live, the headline number, a chart of confirmed results.
- **Understand**: what the target is and how it works, with diagrams. Confirm a topic, flag it, edit it, ask questions.
- **Runs**: every result, claimed against measured. **Tasks**: a board; add work and review it.
- **Pages the project defines**: grids, boards, tables, charts, key numbers and notes. Agents can add them.
- **Bell**: what was found and what needs you, across all projects. **Gear**: agents at once, what happens when
  something needs you, a time limit, and light or dark.

## Running in a VM

Copy this folder in and run `python3 hades.py --no-open`. Agents on the same machine reach it at `127.0.0.1:8765` with no token.

To take reports from other machines:

    PROJECT_VIEW_TOKEN=<secret> PROJECT_VIEW_VERIFIER_TOKEN=<another> python3 server.py --host 0.0.0.0

Agents then set `PROJECT_VIEW_URL` and `PROJECT_VIEW_TOKEN`. You still need your harness installed and signed in on
each machine that runs agents. Hades only starts agents if you create `launchers.json`.

## Trust

- Requests from the server's own machine are fully trusted. Remote writers need the token.
- Only the verifier may post verdicts. The scoring command lives in `projects/<name>/project.json` and can only be set by
  hand or by you approving a proposal on the page.
- Approving a proposal saves an agent-written script that the verifier will run. Read it first.
- An agent with a shell on the server's machine could bypass all of this. For results you need to trust, run the server
  and verifier somewhere the agents cannot reach.

## Files

| File | What it is |
|---|---|
| `hades.py` | The front door: checks the install, then runs the server and the verifier together |
| `server.py` | The server: storage, briefs, tasks, slots, setup and approval |
| `index.html` | The whole page |
| `verifier.py` | Re-scores results with each project's scoring command |
| `loop.py` | Runs any harness in rounds; this one file is all an agent machine needs |
| `emit.py` | Report one event from a shell script |
| `demo.py`, `demo/` | Demo projects with simulated agents and real scoring |
| `toys/` | Ready-made targets. `tinyvm` (the `practice` project) and `blackbox` have hidden answer keys; `stdlib` is real code with none. **The answer keys are spoilers: keep them away from agents.** |
| `projects/` | Your projects' data. Not in version control |

## Known limits

- The page loads a project's whole log. Measured on one machine at 200,000 events (50 MB): it loads in under a second,
  every server call answers in under 0.3 s, and an agent's brief stays near 35 kB. Well past that, or over a slow
  network, it will need paging on the server.
- The verifier scores up to 8 results at once (about 240 a second on the practice project). A scoring command that
  cannot run alongside a copy of itself needs `"workers": 1` under `metric` in its `project.json`.
- Measured coverage adds up counts the scoring command reports. On the `stdlib` target that is inputs tried, not code
  reached, so "covered" there is easy to reach and says little.
- Turn-taking under an agent limit is rough: a waiting agent checks every 15 seconds.
- Desktop notifications, remote tokens and MCP clients are written but lightly tested.
