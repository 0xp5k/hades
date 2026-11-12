# Project View

A live page for long-running AI agent work, and the one port every agent reports to.

Agents (any harness, on any machine) report what they are doing, what they tried and what they found. A separate
verifier re-checks every result, so the page shows what was confirmed, not what was claimed. You read, steer and
approve from the browser.

It needs only Python 3. There is nothing to install.

## Start it

    python3 demo.py          # the page plus three demo projects with simulated agents, to look around
    python3 server.py        # the page on its own, for real projects

Open http://127.0.0.1:8765.

## Start a real project

1. Click **+ Project**, name it, and describe the target in your own words.
2. Start a survey agent with the command the page shows. It studies the target, writes the Understand page so you can
   learn the target too, and proposes a goal, a map of what to cover, and a scoring script.
3. Read the proposal, including the whole scoring script, then approve it or send it back with comments.
4. Start agents. Each round they get a fresh brief: the routine to follow plus where things stand right now.

    python3 verifier.py                                   # re-scores every result; keep it where agents cannot write
    python3 loop.py my-project --agent a-1 -- claude -p   # everything after -- is your own harness command

`loop.py` works with any harness that takes a prompt on standard input or as a file (`{brief_file}`).
`run-live.sh` is a ready-made wrapper for Claude Code; `check-harness.sh` tries Codex or OhMyPi for one round.

## What a harness can call

| Call | What it does |
|---|---|
| `GET  /api/brief?project=X&agent=NAME` | Plain-text instructions plus current state |
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

Copy this folder in and run `python3 server.py`. Agents on the same machine reach it at `127.0.0.1:8765` with no token.

To take reports from other machines:

    PROJECT_VIEW_TOKEN=<secret> PROJECT_VIEW_VERIFIER_TOKEN=<another> python3 server.py --host 0.0.0.0

Agents then set `PROJECT_VIEW_URL` and `PROJECT_VIEW_TOKEN`. You still need your harness installed and signed in on
each machine that runs agents; Project View never starts agents itself.

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
| `server.py` | The server: storage, briefs, tasks, slots, setup and approval |
| `index.html` | The whole page |
| `verifier.py` | Re-scores results with each project's scoring command |
| `loop.py` | Runs any harness in rounds; this one file is all an agent machine needs |
| `emit.py` | Report one event from a shell script |
| `demo.py`, `demo/` | Demo projects with simulated agents and real scoring |
| `toys/` | Practice targets with hidden answer keys. **The answer keys are spoilers: keep them away from agents.** |
| `projects/` | Your projects' data. Not in version control |

## Known limits

- The page loads a project's whole log. It is fine into the tens of thousands of events; beyond that it needs paging on
  the server.
- Turn-taking under an agent limit is rough: a waiting agent checks every 15 seconds.
- Desktop notifications, remote tokens and MCP clients are written but lightly tested.
