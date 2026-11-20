# Hades guide

## Start it

    git clone https://github.com/0xp5k/hades && cd hades
    python3 scripts/hades.py

That one command checks the install (eleven checks, about five seconds, no model needed), starts the page and the verifier
together and opens http://127.0.0.1:8765. The first time, it asks once whether the page's Start button may run Claude
Code on this machine (saying yes copies `launchers.example.json` to `launchers.json`, which you can edit later), and it
creates one empty lean project. Type what to research on the page and press Start.

Or have an assistant do it. Open Claude Code, or any coding agent, in this folder and say:

    Get Hades running for me. Follow "If you are an assistant" in docs/guide.md.

Other ways in:

    python3 scripts/hades.py check   # only the check; exits 0 if everything works
    python3 demo/run.py          # three demo projects with simulated agents, to look around without any model
    python3 server.py        # the page on its own, when the verifier runs somewhere else

## Try it on the practice project

To try a ready-made target, add the practice project with `python3 scripts/hades.py new tinyvm practice`.
It contains a small interpreter with 17 planted defects and a hidden answer key. Start an agent whichever way you like:

    sh run-live.sh practice 3        # in another terminal: one Claude Code agent, three rounds

- **By talking to Claude:** see "Run it by talking to an agent" below.

Watch the page while it works: results arrive on Runs, the verifier confirms or rejects each one, and the map of the
machine fills in from what the verifier measured. Then compare against the answer key:

    python3 toys/tinyvm/answer_key.py score practice

In our own runs Claude Code agents found all 17 in under six minutes. They have the source, so this is the easy one.

Two harder targets are included:

    python3 scripts/hades.py new stdlib      # 25 real parsers from Python's standard library; no answer key

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

    python3 scripts/loop.py my-project --agent a-1 -- claude -p   # everything after -- is your own harness command

`python3 scripts/hades.py` already runs the verifier. If you started `server.py` on its own, run `python3 scripts/verifier.py` too, and
keep it where agents cannot write.

`scripts/loop.py` works with any harness that takes a prompt on standard input or as a file (`{brief_file}`).
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

1. Run `python3 scripts/hades.py check`. If any line says FAIL, stop and show the person that output.
2. Ask the person one question: may the page, and you, start agents on this machine? Saying yes lets the server run the
   agent command in `launchers.example.json`, and nothing else.
3. Start the server in the background, because it does not exit: `python3 scripts/hades.py --skip-check --no-open`, adding
   `--allow-launch` only if they said yes.
4. Read `http://127.0.0.1:8765/api/coordinator` and follow it. It lists your tools and the rules.
5. Tell the person to open http://127.0.0.1:8765. If the workspace includes `practice`, offer to start one agent there. Starting
   agents spends their model usage, so say how many agents and rounds, and wait for a yes.

## Starting agents from the server

Off by default. `python3 scripts/hades.py --allow-launch` switches it on for Claude Code; or copy `launchers.example.json` to
`launchers.json` yourself and edit the command for your harness. Each entry is a
named recipe that the coordinator or launch API can start and stop. Launched agents work in
`agent-work/<project>/<agent>/`, where `loop.log` holds each one's output. A project can name a folder (`agent_files`)
that every new agent starts with a copy of. `loop.pid` there lets a restarted server find a loop it started before:
a new launch skips that name, and stopping the project's agents stops it too.

`launchers.json` is read again at every launch, so you or an agent can change the harness while the server runs (see
"Changing the harness" in `AGENTS.md`). `POST /api/launch` takes an optional `"model"` for that launch only: it replaces the
value after `--model`, `-m` or `--model=` in the recipe, or adds `--model` to a bare `claude` command; a recipe that names no
model and is not `claude` is refused. `GET /api/projects` lists each recipe under `launchers` with its model, the `models`
to choose from, whether it runs live, and its command, plus `launcherError` when part of the file could not be used.
The example recipe allows Claude's file and shell tools without a narrower list. Web access is left out; adding
`WebFetch` or `WebSearch` to `--allowedTools` is your decision.

**Loop settings.** `"loop"` in `project.json` holds the settings every launch on the project uses, set from this machine
with `POST /api/project {"project", "loop": {...}}`. The object replaces the stored one; unknown keys and nulls are dropped
and a bad value is refused with its name. A launch takes each from its request, else `loop`, else the recipe's field of
the same name, else the default. With one recipe it is used without naming it. The launch reply and each `launched`
entry show the resolved `settings`.

| Key | Values | Default |
|---|---|---|
| `recipe` | a recipe name from `launchers.json` | the only recipe, if there is one |
| `model` | a model name for the recipe's command | the recipe's own |
| `rounds` | 0 (until stopped) to 500 | 4 |
| `timeout_min` | minutes per round, 0 (no limit) to 1440 | 0: research rounds run as long as they need |
| `reply_wait` | seconds a round stays open for your reply once you have spoken, 0 to 3600 | 120 |
| `keep_background` | true leaves processes the agent started running after a round | false |
| `style` | `focused` or `explore` | `focused` |
| `agents` | workers Start launches at once, 1 to 5 (the launch request's `count` overrides it) | 1 |
| `tools` | `recipe` uses the recipe's own allow-list; `wide` also allows web search, web fetch and subagents (Claude Code recipes only) | `recipe` |

`wide` adds those tools to the allow-list by name. Permission checks stay on, so connectors and anything else not named
stay refused. To go further, edit the recipe's `command` in `launchers.json`. A chat agent can change any of these for you
when you ask: `AGENTS.md` shows it the `loop_settings` call.

**Round styles.** A `focused` round does one piece of work and settles it. An `explore` round follows its nose: it goes
where the target looks most surprising, takes a quick look at several things rather than proving one, may run things in
parallel, and ends its handoff with a `Leads:` line per lead and the quickest test that would confirm or kill it. Leads
(from any style) become open hypotheses in Research, where you can Pursue or Drop them. Each worker keeps the style it
was launched with; a worker started by hand follows the project's `loop.style`.

**Several workers.** With more than one worker from this server alive on a project, each brief says how many are working
and which number this one is (by name), and starts it from a different open thread, priority threads first. A worker
with no thread left for its number picks an angle the others are unlikely to take. They share no other coordination.

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
| `GET  /api/research?project=X&q=TEXT&area=AREA` | Current research threads, optionally filtered by text or area |
| `POST /mcp` | The same as MCP tools |

A research thread is a report with `type: "research"`, a short `text` claim, `kind` (`hypothesis`, `observation`,
`experiment`, or `conclusion`) and `status` (`open`, `supported`, `contradicted`, `reproduced`, or `superseded`).
Optional fields are `body` for reasoning, `conditions`, `next` for the next useful test, `area`, `scope` for the surface examined,
`snapshot` for a commit, build or version, `run` for the producing run ID, and `evidence` as
`[{"ref": "path, URL, or run id", "note": "what it shows"}]`, and `links` as
`[{"to": "another thread id", "relation": "supports"}]`. Other relations are `contradicts`, `depends on`,
`variant of`, `same cause as`, and `follows from`. Reuse the returned `id` to update a thread. Unspecified evidence,
links, context, conditions and next test survive updates. The Research page offers thread, area and activity views;
each thread shows its attributed updates. `/api/research` returns the current state and a `history` array for each thread.
Each thread also has `priority`, which only you set, from this machine:
`{"project", "type": "research", "id", "priority": true, "source": "you"}` marks it and leaves everything else as it was.
Marked threads come first in `/api/research` and in every brief; an agent's update never changes the mark.
In the same way `{"project", "type": "research", "id", "status": "contradicted", "source": "you"}` drops a thread
(or `"open"` reopens it) without touching its claim or evidence.

## Steering an unattended loop

Every round starts fresh from the brief, so whatever should outlast a round goes there. Each brief leads with:

- **Your direction**: one standing instruction, kept in `project.json` as `direction` and set from this machine with
  `POST /api/project {"project", "direction": "..."}` (up to 4,000 characters; `""` removes it). Every round follows it
  ahead of its own choice of next step until you change it.
- **What you said in the terminal**: each message you send a live worker is also saved to the log (an `activity` with
  `source: "you"` and `harness: "terminal"`), and the latest five appear in every later brief.
- **Research threads you marked**, ahead of the rest.

**Lean brief.** With `"simple": true` (set from this machine with `POST /api/project {"project", "simple": true}`; the
default project made by `server.py --dashboard` has it) the model does research and the harness keeps the record. The
brief holds your direction and messages, the round's last closing note, target, goal and rules, the working rules for its round style, the
current update, marked and open threads with their next test, and the newest 20 ruled-out approaches. It asks for no
reporting. The one call it keeps is submitting a result to the independent check, when `metric.verify` is set.

From each lean round's note, the text after its last `Handoff:` becomes the `now` page's `Current update`. Each
`Results:` line becomes a research thread (kind `conclusion`, status `supported`, `known` from a trailing `[known: ...]`
or `[new]`, `run` when it names an existing run such as "run r12"). Each `Ruled out:` line becomes a contradicted
observation. Derived threads carry `origin: "handoff"`, and repeats of an existing thread are skipped. Each
`Decision needed:` line becomes a question for you. It appears first on the Research page with a box to answer in, and
later briefs give the model your answer as decided and list unanswered ones so it does not ask twice.

**Checker files.** `"protected": ["score.py", "kernel/**"]` in `project.json` (set by hand only, like the scoring command)
lists glob patterns in each agent's folder. The lean brief tells the model not to edit them, and the loop hashes them
around every round and reports a change as `Checker changed during round N: ...`.

## On the page

- The **Project brief** opens first for OpenSSH research. A note section with the ID `now` uses this layout. It shows the project question, live status, and dated update with a link to its records. The OpenSSH workspace instructions tell agents to maintain the same update as the work or your direction changes. Other projects open to Overview or, during setup, Understand.
- **Research** shows a list of claims. Open a thread for its evidence, related claims and history.
- **Experiments** shows outcomes first. Open an experiment for its controls and evidence. Projects without an experiments section show Runs.
- **More** holds the remaining project pages, including sources, notes and history. On a note page, use **Add note** or **Edit**. Note bodies support Markdown.
- To correct a saved note through Codex or Claude, open **Edit update** on the brief or **Edit** on another note page. Choose **Copy reference for agent** and paste it into the chat with your correction. The reference identifies the existing note so the agent can update it in place.
- The header has a project switcher, page search, notifications, and a light or dark toggle.

## Windows

The server, the page and the Start button work on Windows with Python 3 alone: run `py -3 scripts\hades.py`. Workers start
in a process group of their own; Stop ends a worker's whole process tree with `taskkill`, and the loop ends what its rounds
started. A bare program name in a recipe (`claude`) is looked up on PATH first, so `claude.cmd` shims start without a shell.
After a server restart, a worker from before is recognised by its recorded process id and a running Python process, since
Windows does not show other processes' command lines. Scoring commands in `project.json` are split with POSIX rules on
every platform, so quote a Windows path that contains spaces or backslashes, for example `'C:\Python\python.exe' score.py`.

## Running in a VM

Copy this folder in and run `python3 scripts/hades.py --no-open`. Agents on the same machine reach it at `127.0.0.1:8765` with no token.

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
| `scripts/hades.py` | The front door: checks the install, then runs the server and the verifier together |
| `server.py` | The server: storage, briefs, tasks, slots, setup and approval |
| `index.html` | The whole page |
| `scripts/verifier.py` | Re-scores results with each project's scoring command |
| `scripts/loop.py` | Runs any harness in rounds; this one file is all an agent machine needs |
| `scripts/emit.py` | Report one event from a shell script |
| `demo/run.py`, `demo/` | Demo projects with simulated agents and real scoring |
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

## Displaying completed verifier checks

On the Experiments table, a green badge means the verifier confirmed the result on the stated build and configuration for that exact experiment revision. Expand **Verifier record** to inspect the evidence. A check that ran without demonstrating the result shows **Not demonstrated**. A check that could not establish an outcome shows **Check inconclusive**. Agent reports and ordinary run scores cannot award these badges.

Set `HADES_EVIDENCE_DIR` in the dashboard process environment to an absolute directory holding completed reports. Give the dashboard read access. Only the verifier or its operator should have write access. The directory permissions establish the identity of the publisher. Agents sharing the publisher’s OS credentials would share its authority.

The dashboard refreshes `<project-id>.json` in that directory. Publish one complete snapshot per project, replacing the file atomically. Use the experiment key and the full revision shown under **Details**. Include one record per experiment. For example, a completed check might be recorded as follows.

```json
{
  "project": "sample",
  "checks": [{
    "experiment": "Configuration review",
    "revision": "131a0a12320e4a51be75211c26fd2a09",
    "outcome": "inconclusive",
    "build": "release 3.1",
    "configuration": "standard image",
    "evidence": "https://records.example.org/checks/42"
  }]
}
```

The outcome values are `confirmed`, `not-demonstrated` and `inconclusive`. The evidence field holds a URL or an artifact reference. Editing an experiment gives it a new revision and retires its badge. An earlier record in the snapshot stays readable under that experiment. Missing or invalid records cannot produce a confirmation.

## Watching iterations

Open **Live loop** on localhost. For a worker started by Hades, the terminal follows its printed stdout and stderr automatically, including output before the first round event. The viewer reads the newest 64 KiB of the current launch about twice a second. Scroll up to read earlier lines in that window. The full local launch log is saved in the worker’s folder.

A Claude Code worker started by Hades from a direct `claude -p` recipe runs each round as a live session. The terminal shows Claude's text, each tool call and the first lines of each tool result as they happen. Type in the box under the terminal and press Enter to message Claude while it works. Claude reads the message at its next step, in the same session, so you can redirect it without stopping the round. Shift+Enter adds a line.

After you have sent a message, the round stays open for two minutes each time Claude finishes, so you can reply and continue the same conversation. With no reply, the round ends and the loop carries on. A round you never message ends as soon as Claude finishes. Each message restarts the round's time limit. A message sent between rounds reaches Claude when the next round starts.
Every message is also kept for later rounds; see Steering an unattended loop. Choose the model in the start form; it applies
to that launch only. A worker that stops reading for five
seconds gets "not reading messages right now" rather than holding up the server. The round note holds Claude's finished replies, and Run details lists every message with the command it was sent to.

Messages can be sent only from the dashboard open on its own machine, because they reach an agent that can run tools. To keep a recipe as a one-way batch run, add `"interactive": false` to it in `launchers.json`. Recipes for other programs, wrappers and Codex keep the one-way terminal. From your own terminal, add `--interactive` before `--` in a `loop.py` command to get the same live view; each line you type is sent to Claude.

For a separately started worker, browsers with local file access offer **Follow a log file**. Select its growing text log from a disk or share visible to the browser. This selection lasts for the tab session. Programs control when they flush output.

The runner asks for a final handoff and saves the last 6,000 characters of stdout once per round unless round notes are disabled. Terminal viewing requires no progress posts from the model. Select an earlier round for its saved handoff and recorded events. **Follow latest** resumes following new rounds.

Click **ⓘ Run details** beside the terminal title to read the CLI arguments and exact text submitted on standard input. For a saved round, the same button appears beside its iteration details. Prompt text passed as an argument is visible in the command. These records describe input at the local CLI boundary. Provider-managed system instructions and context added inside the CLI are outside that boundary.

Hades stores these records locally for dashboard-launched rounds and serves them on localhost. The live terminal stays visible when you close the dialog. On macOS and Linux, wrappers that invoke a batch `claude` or `codex` by name through PATH are captured automatically. A wrapper that bypasses PATH, including one with an absolute executable path, can forward its final call through the capture helper on any platform. For a Python wrapper the call is `subprocess.run([sys.executable, os.environ["HADES_CAPTURE_CLI"], "--capture-cli", executable, *args], input=final_prompt_bytes)`. The wrapper’s original command and input appear separately. Older or uninstrumented runs show which capture is missing.
