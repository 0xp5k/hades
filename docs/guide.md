# Hades guide

Hades is four things.

- **A loop.** Each round starts an agent (Claude Code, Codex, any command) with no memory, hands it a brief, and saves
  the handoff it ends with.
- **A record.** One append-only log per project. Everything else is derived from it.
- **Gates.** Tests a result must pass before it counts. The round that made a result never judges it.
- **A window.** The same facts as a page for you and as plain text for an assistant.

Nothing in it waits for you. The loop picks its own next piece of work, and where only you could decide it takes the
cautious option and writes down what it assumed. You start it from the page and read the page. To steer it or have it
explained, talk to an assistant (Claude, Codex) and point it at `GET /api/status?project=X`; `AGENTS.md` is its manual.

## Start it

    git clone https://github.com/0xp5k/hades && cd hades
    python3 scripts/hades.py

That checks the install (eight checks, a few seconds, no model), starts the server and opens http://127.0.0.1:8765. The
first time, it asks once whether the page's Start button may run an agent on this machine; yes writes `launchers.json`
with a recipe for each of Claude Code, Codex and OhMyPi it finds. Press Copy for Claude, tell your assistant what to
research (it sets the goal), and press Start loop.

    python3 scripts/hades.py check   # only the check; exits 0 if everything works
    python3 server.py                # the server on its own

The server listens on this machine only, and everything that can reach it is trusted. Run it where you would run the
agents themselves.

## The words

- **Frame**: a place to look, not yet settled.
- **Claim**: what a round says it found. **Ruled out**: a frame that was examined and had nothing.
- **Gate**: a test a claim must pass. **Established**: a claim that passed every gate.
- **Extra check**: a gate that is not required. It runs only on what is established and never stops a claim.
- **Round**: one run of the agent. **Handoff**: the block it ends with.
- **Assumption**: something a round decided in your place.

These are the only words the brief, the digest, the page and this guide use for them.

## One round

1. The loop stops if it has reached its rounds, its hours or its spend limit, or a file named `STOP` is in its folder.
   It waits while the project is paused.
2. It fetches the brief. The round chooses where to go. The first frame in the queue is offered as a place to start,
   and the other queued frames are listed as leads it may take instead. Only a frame the person put first is one the
   round is told to start on.
3. It runs the agent's command with the brief on standard input (`{brief_file}` and `{brief}` in an argument are
   replaced with a file holding it and with the text). Nothing is sent to a round after its brief.
4. It saves the round's output from its last `Handoff:` marker on as that round's **note**. The record is made from it.
5. It runs every gate that is due on what the round claimed, then goes again.

Every round is asked to end with:

    Handoff: <what you did and what you observed>
    Results: <one line per result claimed, each ending [check: artifact]>
    Ruled out: <one line per approach that does not work, with the reason>
    Assumed: <anything only the person could have decided, and the cautious choice made instead>
    Leads: <one line per new place worth looking, each with its quickest test and [score: 1-4]>
    Next: <the single most useful next step>

Each `Results:` line becomes a claim. Each `Ruled out:` line is recorded so the ground is not covered twice. Each
`Leads:` line becomes a frame. Each `Assumed:` line stays in force until you overrule it. A line that starts with an id
in brackets, like `[k12]`, settles that frame or claim in place. A Results line ending `[known: where]` is recorded as
ruled out, with the reference. Headings, bold labels, numbered lists and colour all parse. A round with no Handoff
marker records nothing, and the log says so.

## The brief

The brief says what counts and what is known. It does not say how to work: in the real runs the useful results came
from the agent's own ideas, so a test fails if the brief grows. It holds, in order:

- your direction, if you set one;
- the round's own last handoff, when that was about the same thing;
- the target, goal and rules;
- one line on what the round is for (settle the frame named below, or find what is worth finding);
- the gates, with the commands a round may run itself;
- what you decided, and assumptions in force;
- what is established; what is already known; claims a gate stopped, with what the gate said;
- titles of the other claims, the queue, frames set aside, and the newest ruled-out approaches;
- last, this round's frame.

The loop adds where the round is working, the time limit if there is one, and the handoff format.

## Frames

- **Where they come from.** A `Leads:` line in any handoff, or `POST /api/frames` with a list.
- **Score.** 1 to 5: how many rounds a frame is worth; 3 if nobody said; a round's own lead is capped at 4. A round
  spends one when it starts on the frame. When they are spent the frame is **set aside** and leaves the queue.
- **Order.** Frames you marked first, then the highest score, then the oldest.
- **A claim a gate stopped is not queued.** It is listed in every brief with what the gate said, and any round may
  repair it and claim it again by its id. Marking it gives it a round of its own.

## Gates

A project with no `gates` gets one built in, `refutation`: a fresh round is shown only the claim and told to refute it.
It needs no setup and is the weakest kind. `"gates": []` turns gating off.

For stronger checking, edit the project's `project.json` and put each gate's script in that folder. No API sets gates,
so a round cannot change them through the server.

    "artifact": "the path of a file holding the input that triggers the fault",
    "lock": ["scope.md", "replay.py"],
    "gates": [
      {"name": "reproduces", "command": "python3 {project}/replay.py"},
      {"name": "not reported", "recipe": "claude-reader", "refute": "Find an earlier report of this fault. Cite what you opened."},
      {"name": "matters", "rate": "How much would a maintainer of this code care, and why?"}
    ]

- **A command.** Run with the claim's artifact as its last argument (or where you write `{artifact}`). `{project}` is
  the project's folder. Exit code 0 passes. One that cannot start or runs out of time (`timeout_min`, default 10) is
  run again, three times in all. On Windows a `.cmd` or `.bat` gate is refused.
- **A refuter.** A fresh round given only the claim, its artifact and your instruction. It ends `Verdict: refuted`,
  `Verdict: known` (already reported: the claim is stopped for good) or `Verdict: stands`.
- **A rater.** `"rate"` in place of `"refute"`. It ends `Rating: 1-5`. It never stops a claim; it orders what is
  established, and runs after everything else.

**Extra checks.** Add `"required": false` to a command or a refuter and it becomes an extra check: another verifier
that is not part of what every claim must pass. It runs only on claims the required gates have established, and
whatever it says the claim stays established. Its verdict is shown beside the others, and it adds a count of its own
beside Established: the established claims that also passed it. Use it for a stronger test that only some results will meet: the proof also checks
formally, the fault also reproduces on the release build, the fix also passes the project's tests.

    {"name": "formal proof", "command": "python3 {project}/lean.py", "required": false}

A refuter or rater may name a `"recipe"` from `launchers.json` to run on a different agent: another model's check, or
one with web access.

**Which run when.** Every due gate runs after each research round, so the next brief carries the verdicts. A refuter
or rater is a whole round of a model, so make a check a command whenever a program can decide it, and among gates of
the same cost put the one that rejects most first.

**What verdicts are bound to.** Each verdict is stamped with its own gate and the files `lock` names. Reword a gate
and its verdicts are stale; change a locked file and all are. A verdict counts only for the artifact the claim names
now.

Good gates, cheapest first: *it is true* (replay under a sanitizer in a clean container, build the proof, rerun from
the seed); *it is the thing asked*; *it is new* (a refuter that must cite the earlier report); *it is usable*. A gate
sees the artifact name as the agent wrote it, so treat it as untrusted, and run what a round produced in a container
when it could do harm.

## Running it

Loop settings live in `project.json` under `loop` and are set from Settings or `POST /api/project`:

| Setting | Values | Default |
|---|---|---|
| `recipe` | a name from `launchers.json` | the only one, if there is one |
| `model` | a model name for the recipe's command | the recipe's own |
| `rounds` | 0 (until stopped) to 500 | 4 |
| `timeout_min` | minutes per round, 0 (no limit) to 1440 | 0 |
| `workers` | 1 to 4 loops side by side | 1 |
| `spend` | the most model rounds one launch may use, research and checking together; 0 for no limit | 0 |
| `keep_background` | leave processes the agent started running between rounds | false |

With `workers` above 1 a launch starts that many loops of the recipe (`<recipe>-1`, `<recipe>-2`). They share the
record and nothing else: each has its own folder, is given a different frame, and runs the gates on its own claims.

**Adding and removing loops while it runs.** Add a loop on the Overview, or `POST /api/launch {"project", "add": true}`,
starts one more loop beside those running and leaves them as they are, up to four on a project. It runs under the
same settings unless you send others, and `"recipe"` there starts a different agent beside the first, which is how to
put a second model on the same record. `POST /api/stop {"project", "agent": "<name>"}` stops one and leaves the rest.

**One spend limit.** The server counts the model rounds a launch has used and every loop asks before each one, so the
limit holds however many loops there are and whenever they joined. It can be passed by the rounds already under way
when it is reached. Hades has no clock: to run more at night, have an assistant or a scheduler send the launch then.

A recipe in `launchers.json` has `command` (the argument list run each round), and optionally `label`, `models`,
`rounds` and `timeout_min`. Tools and permissions are whatever the command says. A direct `claude -p` command is shown
as it works; any other program's output is shown as it prints.

To run the loop yourself, from the folder the agent should work in:

    python3 /path/to/hades/scripts/loop.py my-project --agent claude-1 -- claude -p

## The page

Four pages. **Overview**: whether it is running, what it is doing, what is established, what it cost. **Research**:
every frame and claim in the group the record puts it in, with each gate's verdict. **Live**: the loop's output as it
happens, and each earlier round's handoff. **Write-ups**: see below. The controls are Start, Stop, Add a loop, Settings and Copy for
Claude. Everything else is done through an assistant.

**When the record gets long.** A group with more than eight items is drawn as tight rows, twenty at a time, with
Show more and Show all. Each tight row shows only what tells it apart: the gate it stopped or waits at, what the extra
checks said, its rating. Ruled out, Set aside and Dropped start closed once they are long. Past two dozen items the
Research page gets a bar of group names that stays in view and jumps to each. Search narrows every group at once. The
brief and the digest cut their lists short too, and say how many they left out.

**Digging into one claim.** Details shows each gate's last line, with the rest of its output one press away, and
Show the file shows the claim's artifact as it is on disk (text up to 64 kB, and only from the project's own working
folders).

**Handing a result to someone else.** Copy as report, on an established claim, copies it written up in Markdown: what
was found, the input, the command that shows it again, and what each gate said. The server assembles it from the
record, so no model spends a round on it, and this machine's paths are taken out.

**Since you last looked.** When you come back to the Overview it says what changed while you were away: how many more
are established, stopped, ruled out, and how many rounds ran. The earlier counts are kept in your browser only.

**Run details** beside the terminal shows the command and the exact text given to the agent for a round.

**Write-ups** holds explanations your assistant wrote for you: how the target works, what a result means. Ask for
one in plain words. Each is a Markdown file in `projects/<id>/writeups/`, so it can be shared, edited and kept like any
document. A write-up is not part of the record: no round reads it and no gate checks it.

## The API

| Call | What it does |
|---|---|
| `GET  /api/status?project=X` | Plain text: where the project stands and what can be done. Read this first |
| `GET  /api/brief?project=X&agent=NAME` | The brief one round would get |
| `GET  /api/research?project=X` | Every frame and claim with where it stands |
| `GET  /api/events?project=X&after=N` | The log, 5,000 events at a time |
| `GET  /api/project?project=X`, `/api/projects` | Config; every project with its running loops and recipes |
| `GET  /api/gates?project=X&agent=NAME` | What the loop should run next |
| `GET  /api/writeup?project=X&name=N` | One write-up, with its text |
| `GET  /api/artifact?project=X&id=K` | The file a claim names as its artifact, for reading |
| `GET  /api/report?project=X&id=K` | One established result written up in Markdown, from the record |
| `POST /api/frames` | Queue places to look |
| `POST /api/events` | An `activity`, a `note`, a `question`, or `research` |
| `POST /api/project` | Set `goal`, `rules`, `target`, `direction`, `paused` or `loop` |
| `POST /api/new` | Create a project: `{"project", "goal", "target", "agent_files"}` |
| `POST /api/launch`, `/api/stop` | Start and stop the project's loops; `"add": true` starts one more, `"agent"` stops one |

`/api/research` returns each thread with `status` (`open` a frame, `supported` a claim, `contradicted` ruled out,
`superseded` dropped), its `gates` verdicts (each with `live`: whether it still counts), and where it stands: `bucket`
(established, stopped, waiting, unchecked, ruled, queued, aside, dropped), `state`, `at`, `standing`, `rating`,
`place` in the queue, and `working` (the agent on it now).

Writes must be JSON from this machine; a request from another site is refused.

## Windows

Run `py -3 scripts\hades.py`. The loop starts in its own process group and Stop ends its whole tree. A bare program
name in a recipe is looked up on PATH first, so `claude.cmd` shims start without a shell.

## Files

| File | What it is |
|---|---|
| `scripts/hades.py` | The front door: checks the install, then starts the server |
| `server.py` | The record, the brief, the gates' bookkeeping, launching |
| `scripts/loop.py` | Runs an agent in rounds and runs the gates |
| `index.html` | The page |
| `launchers.json`, `projects/`, `agent-work/` | Your recipes, your records, the folders loops work in. Not in version control |

## Known limits

- An agent with a shell on this machine can write to the record as anyone and can edit a gate or a locked file. Nothing
  here is a security boundary between the agent and the record. For a result that matters, run the gates yourself.
- A verdict is about an artifact by name. If the file's contents change afterwards, nothing notices
  until the claim is made again.
- A refuter is a model judging a model. A claim it lets through has only failed to be killed.
- Spend is counted in model rounds, not tokens or money, and only for loops started from the page or the API.
- If `project.json` stops being valid JSON the loop holds and says why. That is the one thing it waits for.
