# Hades guide

Hades is a research loop with a page on top. Each round starts Claude (or another harness) fresh, hands it a brief
generated from the project's record, and saves the round's closing handoff back into that record. The next round's brief
is built from it.

Nothing in it waits for you. The loop picks its own next piece of work, has every result attacked before it counts, and
when it needs a decision only you could make it takes the cautious option and writes down what it assumed. You use the
page to start it and to see everything. To steer it or have it explained, talk to Claude and point it at the server:
`AGENTS.md` is its manual, and `GET /api/status?project=X` is what it reads first.

## Start it

    git clone https://github.com/0xp5k/hades && cd hades
    python3 scripts/hades.py

That one command checks the install (eight checks, a few seconds, no model needed), starts the page and opens
http://127.0.0.1:8765. The first time, it asks once whether the page's Start button may run an agent on this machine
(saying yes writes `launchers.json` with a recipe for each of Claude Code, Codex and OhMyPi it finds installed, which you
can edit later), and it creates one empty
project. Type what to research on the page and press Start.

    python3 scripts/hades.py check   # only the check; exits 0 if everything works
    python3 server.py                # the page on its own

The server listens on this machine only. Everything that can reach it is trusted, so run it where you would run the
agents themselves.

## One round

1. The loop stops if it has reached its round or time limit, or a file named `STOP` exists in its folder. It waits while
   the project is paused.
2. It fetches the brief: `GET /api/brief?project=X&agent=NAME`. While a frame is queued, the brief gives the round that
   one frame to settle. When none is, the round widens: it looks for places worth looking and proposes them.
3. It runs the harness command with the brief on standard input (`{brief_file}` and `{brief}` in an argument are
   replaced with a file holding it and with the text).
4. It saves the last 6,000 characters the harness printed as a note on the Round notes page.
5. If the project has gates, it runs them in order on each result the round claimed with an artifact, then goes again.

Every round is asked to end with this block:

    Handoff: <what you did and what you observed>
    Results: <one line per result claimed; [known: reference] or [new] at the end of a line>
    Ruled out: <one line per approach that does not work, with the reason>
    Assumed: <anything only you could have decided: the cautious choice it made instead>
    Leads: <one line per new place worth looking, each with its quickest test and [score: 1-5]>
    Next: <the single most useful next step>

The server keeps the record from it. The text after the last `Handoff:` becomes the Project brief's `Current update`.
Each `Results:` line becomes a research thread (kind `conclusion`, status `supported`, with `known` from its tag). Each
`Ruled out:` line becomes a contradicted thread. Each `Leads:` line becomes an open thread, a frame for a later round.
Each `Assumed:` line is kept as an assumption in force: later rounds keep to it until you overrule it. A line that starts with a thread's id in brackets, like
`[k12]`, settles that thread in place instead of adding one. Repeats of an existing thread or an assumption already in force are
skipped. The id has to start the line; one in the middle of a sentence is read as prose. A heading works as well as a
colon (`## Results`). A round whose output has no Handoff line records nothing, and the log says so.

## How the pieces fit

There is one kind of record, the thread, and it moves through three stages.

- A **frame** is a place to look: an open thread. Leads become frames; you can add them; a list can be queued at once.
- A round settles a frame into a **claim** (a result, ideally with an artifact) or **rules it out** (with what was
  examined, so the ground is not covered twice).
- A claim with an artifact goes through the project's **gates**. One that passes them all is **established**.

The loop always works the best open frame. The page always shows the claims that got furthest first. Everything below
is detail on those two sentences.

## Gates

A result is the agent's claim until something outside the round that made it tests it. Gates are those tests, in the
order a claim must pass them.

**You do not have to set any up.** A project with no `gates` of its own gets one built in, called `refutation`: after
each round, a fresh round is shown only the claim and told to make it fail three ways (make it happen again, hold it
against the goal as stated, find where it was already reported). It costs one extra round per claim, at most four per
research round, and it is the weakest kind of gate. `"gates": []` turns gating off.

For stronger checking, add your own to the project's `project.json`, or ask Claude to write them. The API cannot set
them, so a round cannot change them through the server.

    "artifact": "the path of a file holding the input that triggers the fault",
    "lock": ["scope.md", "replay.py"],
    "gates": [
      {"name": "reproduces", "command": "python3 {project}/replay.py"},
      {"name": "default build", "command": "python3 {project}/default_build.py", "timeout_min": 20},
      {"name": "not known", "refute": "Look for an earlier report of this exact fault. It is refuted only if you can cite one."}
    ]

A gate is one of three things.

- **A command.** It is run once per claim, with the artifact the claim names as its last argument (or wherever you write
  `{artifact}`). It is a list of arguments, or one string split with POSIX shell rules. `{project}` is the project's own
  folder under `projects/`, which is where a gate's script and anything it compares against should live, outside the
  folder the agent works in. **Exit code 0 passes; anything else fails.** A gate that cannot start or runs out of time
  (`timeout_min`, default 10) is recorded as "could not run", and is run again after later rounds, three times in all,
  before the claim is left there. On Windows write paths with forward slashes or use the list form, and make the gate
  an `.exe` or a script run by an interpreter: a `.cmd` or `.bat` gate is refused, because `cmd.exe` would read the
  artifact's name as part of a command line.
- **A refuter.** The loop starts the same harness fresh and gives it only the claim, its artifact and your instruction.
  It has not seen how the claim was reached. It must end with `Verdict: refuted - <concrete evidence>` or
  `Verdict: stands - <what it tried>`. Use this where no command can decide: has it been reported before, does it answer
  the question as you meant it. A refuter that finds nothing is weaker evidence than a command that passes, so put
  command gates first.

- **A rater.** Written with `"rate"` in place of `"refute"`. A fresh round is shown a claim that has passed everything
  before it and says how much it matters, ending `Rating: <1 to 5> - <why>`. A rater never stops a claim. Its number
  orders what is established, on the page, in the digest and in the brief. Put it last. It is told to argue the result
  down first, because whoever finds a result overrates it; it is still a model's opinion, and your audit outranks it.

A refuter or a rater may add `"recipe": "<name from launchers.json>"` to run on a different agent from the one doing the
research. Two reasons to: a check from another model does not share the first one's blind spots, and a refuter that has
to look something up needs tools the research agent may not have, such as web access.

    {"name": "not known", "recipe": "claude-reader", "refute": "Find an earlier report of this result. Cite what you opened."},
    {"name": "matters", "recipe": "claude-reader", "rate": "How much would a maintainer of this code care, and why?"}

How a claim moves:

- Every Results line is a claim, and every claim is gated. The agent ends a line with `[check: <artifact>]` to name
  what should be checked; `artifact` in `project.json` tells it what to name. A command gate needs one. A refuter can
  work from the claim alone.
- After the round has ended, the loop runs the first gate in the folder that round worked in, where its artifact is. A
  pass makes the next gate due. The first gate that fails stops the claim there. The agent never reports its own
  verdict. A verdict counts only for the artifact the claim names now.
- The last of each gate's output is kept on the thread.
- A claim that a gate stopped becomes work again. The next brief shows what the gate said, and a round can repair it and
  claim it again by its id, which runs every gate again on the new artifact, or rule it out.

**Verdicts are bound to the question.** `lock` names files in the project's folder: the exact statement of what you are
asking, the scope, a gate's own script. Every verdict is stamped with a fingerprint of the gates and those files. Change
any of them and every earlier verdict is stale: it counts for nothing, the page shows the gate as due again, and the
loop reruns it after the next round. A gate that was running while you changed them is thrown away and run again. So
you can sharpen the question without old passes surviving it. Rewording a claim does the same to that claim.

**Your audit.** On any claim, **Mark as audited** records that you looked at it yourself. It lapses when the claim's
artifact, the gates or the locked files change.

What good gates look like, cheapest first:

- *It is true.* Replay the input under a sanitizer in a clean container. Build the proof with no holes. Rerun the
  experiment from its seed.
- *It is the thing asked.* The fault is reached from untrusted input in a default build. The theorem's statement is the
  locked one, character for character. The benchmark is the agreed one.
- *It is new.* A refuter that must cite the earlier report, advisory or paper.
- *It is usable.* The proposed fix stops the reproduction and the project's own tests still pass.

A gate sees the artifact name exactly as the agent wrote it, so treat it as untrusted input.

## Frames

While a frame is queued, each round is given one and asked to settle it. That keeps a fresh round from drifting back to
whatever the last one found, and it makes "what is the loop doing next" a question with an answer. When the queue is
empty the round widens instead: it follows its nose and ends with new leads, each scored. So the loop alternates on its
own between finding places to look and settling them, and never stops to ask.

- **Where frames come from.** A `Leads:` line in any handoff. The **Add thread** button. Or a list, all at once:

      curl -s -X POST http://127.0.0.1:8765/api/frames -H 'Content-Type: application/json' \
        -d '{"project": "my-project", "frames": ["src/parse.c: length fields", {"text": "src/net.c: framing", "score": 5}]}'

  An entry that repeats an existing thread is skipped.
- **Score.** Each frame has a score from 1 to 5: how many rounds it is worth. A lead can carry one, `[score: 4]`, at the
  end of its line; a queued frame can carry one; otherwise it is 3. A round spends one when it starts on the frame, so a round that crashes or prints nothing counts.
  When they are spent the frame is **set aside**: it leaves the queue. Later rounds are told it was tried, and to come
  back to it only as a new lead with a new angle.
- **Order.** Frames you marked with **Pursue** first. Then whatever is furthest through the gates: a claim stopped at a
  later gate is nearer a result than a fresh frame, while one stopped at its first gate ranks with the frames. Then the
  highest score. Then the oldest. The Research page lists open leads in this order and marks the one that is next.
- **Pursue** puts a frame at the front and gives it its rounds to spend again. **Drop** removes it.
- **What the round is told.** The frame, its quickest test, and that it must end one of two ways: a Results line that
  starts with the frame's id, or a Ruled out line that starts with it and says what was examined. With gates behind it,
  the round is also told to work as if the frame pays off. The gates are what make that safe: an assertive brief always
  produces an answer, and something has to be able to say no.
- **When the frame stays open,** the round's `Next:` line is saved on the frame, so the next round on it starts there.

## The brief

Each brief is generated fresh and holds, in order:

- **Your direction**: one standing instruction, kept in `project.json` as `direction`. Every round follows it ahead of
  its own choice of next step until you change it.
- **What you said in the terminal**: the latest five messages you sent a live round.
- A warning when the last three rounds recorded nothing new, asking for a different angle.
- The round's own closing note from last time, then the target, goal and rules.
- The working rules for the round (settle one frame, or widen), and the gates with the commands a round may run itself.
- What you decided, and the assumptions earlier rounds made in your place.
- The current update, when another agent wrote it.
- What is established. Then each claim a gate stopped, with what the gate said.
- Titles only: other claims, the frames queued behind this one, and frames set aside.
- The newest twenty ruled-out approaches.
- Last, this round's frame.

It asks for no reporting.

## On the page

- **Project brief** shows the project's question, live status and the dated current update. Use **Edit update** to
  correct it, or **Copy reference for agent** and paste that into a chat with your correction.
- **Live loop** has the goal, your direction, Start and Stop, the loop settings, the terminal and each round's saved
  handoff.
- **Research** leads with the gates, then four groups. **Established** holds what passed every gate. **Claimed, not
  established** is ranked by gates passed, with a pill per gate. **Ruled out** keeps the reasons. **Open leads** are in
  the order the loop takes them, with the next one marked and each one's rounds shown. Questions the agent needs you to
  decide appear first with a box to answer in. Open a thread for each gate's output, its evidence and history.
  **Pursue** puts a thread at the front; **Drop** removes it. You can add a thread yourself.
- **Library** holds explanation pages that a chat agent writes when you ask it to explain something (see `AGENTS.md`),
  and the project's note pages.
- **More** holds the remaining pages, including Round notes, and **New project**.

## Starting the loop from the page

Off until `launchers.json` exists. `python3 scripts/hades.py --allow-launch` creates it with a recipe for each agent
program installed here; or copy `launchers.example.json` yourself and edit the commands. With more than one recipe,
choose which a project uses in its Settings.

| Agent | Recipe command | How the brief reaches it |
|---|---|---|
| Claude Code | `claude -p --model opus --allowedTools Bash Read Write Edit Glob Grep` | standard input; rounds are live and can be messaged |
| Codex | `codex exec --skip-git-repo-check -s workspace-write -c approval_policy="never" -` | standard input; it may write only inside its working folder |
| OhMyPi | `omp -p --no-session --auto-approve @{brief_file}` | a file, named with `@` |

Any program that takes a prompt on standard input or as a file works the same way, refuter gates included. Each entry is a named recipe (see "Changing the
harness" in `AGENTS.md`). The file is read again at every launch.

A project runs one loop at a time: Start is refused while one is alive, and Stop ends the loop and everything its round
started. The loop works in `agent-work/<project>/<recipe>-1/`, where `loop.log` holds its output. A project can name a
folder (`agent_files`) that a new working folder starts with a copy of. `loop.pid` there lets a restarted server find a
loop it started before, so it can still be stopped.

**Loop settings.** `"loop"` in `project.json` holds the settings every launch on the project uses, set from the page or
with `POST /api/project {"project", "loop": {...}}`. The object replaces the stored one; unknown keys and nulls are
dropped and a bad value is refused with its name. A launch takes each from its request, else `loop`, else the recipe's
field of the same name, else the default.

| Key | Values | Default |
|---|---|---|
| `recipe` | a recipe name from `launchers.json` | the only recipe, if there is one |
| `model` | a model name for the recipe's command | the recipe's own |
| `rounds` | 0 (until stopped) to 500 | 4 |
| `timeout_min` | minutes per round, 0 (no limit) to 1440 | 0: research rounds run as long as they need |
| `reply_wait` | seconds a round stays open for your reply once you have spoken, 0 to 3600 | 120 |
| `keep_background` | true leaves processes the agent started running after a round | false |
| `tools` | `recipe` uses the recipe's own allow-list; `wide` also allows web search, web fetch and subagents (Claude Code recipes only) | `recipe` |

`wide` adds those tools to the allow-list by name. Permission checks stay on, so connectors and anything else not named
stay refused. To go further, edit the recipe's `command` in `launchers.json`.

To run the loop yourself instead, from the folder the agent should work in:

    python3 /path/to/hades/scripts/loop.py my-project --agent claude-1 -- claude -p

Everything after `--` is your own harness command. Add `--interactive` before `--` for the live two-way view described
below, `--max-iterations N`, `--max-hours H` or `--timeout-min M` for limits.

## Watching and steering a round

Open **Live loop**. The terminal follows the launched loop's printed output, including output before the first round
event. The viewer reads the newest 64 KiB of the current launch about twice a second. The full log is saved in the
loop's folder.

A Claude Code loop started from a direct `claude -p` recipe runs each round as a live session. The terminal shows
Claude's text, each tool call and the first lines of each tool result as they happen. Type in the box under the terminal
and press Enter to message Claude while it works. Claude reads the message at its next step, in the same session, so you
can redirect it without stopping the round. Shift+Enter adds a line.

After you have sent a message, the round stays open for two minutes each time Claude finishes, so you can reply and
continue the same conversation. With no reply, the round ends and the loop carries on. A round you never message ends as
soon as Claude finishes. Each message restarts the round's time limit. A message sent between rounds reaches Claude when
the next round starts. Every message is also kept for later rounds' briefs. A loop that stops reading for five seconds
gets "not reading messages right now" rather than holding up the server.

Messages can be sent only from the dashboard's own page, because they reach an agent that can run tools. To keep a
recipe as a one-way batch run, add `"interactive": false` to it in `launchers.json`. Recipes for other programs,
wrappers and Codex keep the one-way terminal.

For a loop you started yourself, browsers with local file access offer **Follow a log file**. Select its growing text
log. This selection lasts for the tab session.

Select an earlier round for its saved handoff and recorded events. **Follow latest** resumes following new rounds.

**Run details.** Click **ⓘ Run details** beside the terminal title to read the CLI arguments and exact text submitted on
standard input. For a saved round, the same button appears beside its iteration details. These records describe input
at the local CLI boundary. Provider-managed system instructions and context added inside the CLI are outside that
boundary.

Hades stores these records locally for dashboard-launched rounds. On macOS and Linux, wrappers that invoke a batch
`claude` or `codex` by name through PATH are captured automatically. A wrapper that bypasses PATH, including one with an
absolute executable path, can forward its final call through the capture helper on any platform. For a Python wrapper
the call is `subprocess.run([sys.executable, os.environ["HADES_CAPTURE_CLI"], "--capture-cli", executable, *args], input=final_prompt_bytes)`.
The wrapper's original command and input appear separately. Older or uninstrumented runs show which capture is missing.

## The API

| Call | What it does |
|---|---|
| `GET  /api/status?project=X` | Plain text: where the project stands. What an assistant reads first |
| `GET  /api/brief?project=X&agent=NAME` | The plain-text brief for one round |
| `GET  /api/project?project=X` | The project's config, and `hold` when it is paused |
| `GET  /api/projects` | Every project with its launched loop and the launch recipes |
| `GET  /api/events?project=X&after=N` | The log, 5,000 events at a time |
| `GET  /api/research?project=X&q=TEXT&area=AREA` | Current research threads, optionally filtered |
| `GET  /api/gates?project=X` | The claims waiting for a gate, and what to run on each |
| `POST /api/frames` | Queue places to look, many at once |
| `POST /api/events` | Report an `activity`, `item`, `question`, `research` or `topic` |
| `POST /api/project` | Set `goal`, `summary`, `rules`, `target`, `direction`, `paused`, `loop`, or add a `section` |
| `POST /api/new` | Create a project: `{"project", "goal", "target", "agent_files"}` |
| `POST /api/launch`, `/api/stop` | Start and stop the project's loop |

A research thread is a report with `type: "research"`, a short `text` claim, `kind` (`hypothesis`, `observation`,
`experiment`, or `conclusion`) and `status` (`open`, `supported`, `contradicted`, `reproduced`, or `superseded`).
Optional fields are `body` for reasoning, `conditions`, `next` for the next useful test, `area`, `scope` for the surface
examined, `snapshot` for a commit, build or version, `evidence` as `[{"ref": "path or URL", "note": "what it shows"}]`,
`artifact` (what the gates are run on), `score` (1 to 5, the rounds a frame is worth), and `links` as
`[{"to": "another thread id", "relation": "supports"}]`. Other relations are `contradicts`, `depends on`,
`variant of`, `same cause as`, and `follows from`. Reuse the returned `id` to update a thread. Unspecified evidence,
links, context, conditions and next test survive updates. `/api/research` returns the current state and a `history`
array for each thread, with where it stands: `state` (`established`, `failed`, `error`, `waiting` or `unchecked`), `at`
(the gate it is stopped or waiting at), `standing` (gates passed in order), and as work `frame`, `attempts`, `budget`
and `stalled`.

`{"project", "type": "research", "id", "priority": true, "source": "you"}` marks a thread and leaves everything else as
it was. Marked threads come first in `/api/research` and in every brief; an agent's update never changes the mark. In
the same way `{"project", "type": "research", "id", "status": "contradicted", "source": "you"}` drops a thread (or
`"open"` reopens it) without touching its claim or evidence. `"score": 4` and `"audited": true` are sent the same way.

To overrule an assumption: `{"project", "type": "question", "id", "answer": "...", "source": "you"}`.

A project can have extra pages. `POST /api/project {"project", "section": {"id", "title", "view"}}` adds one; `view` is
`note` (one block of prose per key) or `table` (one row per key, with `columns`). Fill it with `item` reports:
`{"project", "type": "item", "section", "key", "body"}` or `"data": {"<column>": value}`.

## Windows

The server, the page and the Start button work on Windows with Python 3 alone: run `py -3 scripts\hades.py`. The loop
starts in a process group of its own; Stop ends its whole process tree with `taskkill`, and the loop ends what its
rounds started. A bare program name in a recipe (`claude`) is looked up on PATH first, so `claude.cmd` shims start
without a shell. After a server restart, a loop from before is recognised by its recorded process id and a running
Python process, since Windows does not show other processes' command lines.

## Files

| File | What it is |
|---|---|
| `scripts/hades.py` | The front door: checks the install, then starts the server |
| `server.py` | The server: the log, the brief, the handoff record, launching |
| `index.html` | The whole page |
| `scripts/loop.py` | Runs a harness in rounds |
| `launchers.json` | Your launch recipes. Not in version control |
| `projects/` | Your projects' data. Not in version control |
| `agent-work/` | The folders loops work in. Not in version control |

## Known limits

- The page loads a project's whole log.
- An agent with a shell on this machine can write to the record as anyone, including as you or as a gate, and can edit
  a gate or a locked file. Nothing here is a security boundary between the agent and the record. For a result that
  matters, run the gates again yourself, then mark it audited.
- A verdict and your audit are about an artifact by name. If the file's contents change afterwards, nothing notices
  until the claim is made again.
- If `project.json` stops being valid JSON, the loop holds and says why, and the server refuses to write to it. That is
  the one thing it waits for.
- A refuter is a model judging a model. Its refutation has to be concrete to count, but a claim it lets through has
  only failed to be killed.
- There is no pass that scores a queue for you, no running of several attempts side by side, and no tracking of what
  happened to a result after you reported it.
- A log written by an earlier version may hold kinds of report that no longer exist (runs, verdicts, findings, tasks).
  They stay in `events.jsonl` and are skipped when it is loaded.
