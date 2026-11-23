# Working with Hades

Hades runs a research loop and keeps its record. This file is for an assistant the person is talking to (Claude, Codex, any other): how to see what the loop is doing, explain it, and steer it for them. The server is local and defaults to http://127.0.0.1:8765; use the address from the page or the context if it differs.

**If you are a round inside the loop** (you were started with a brief that ends in a Handoff block), none of this is for you. Do what the brief says, end with the handoff, and do not call the server.

## The words

Use these, and only these, when you report.

- **Frame**: a place to look, not yet settled. Leads from a round become frames; you can queue them.
- **Claim**: what a round says it found. **Ruled out**: a frame that was examined and had nothing.
- **Gate**: a test a claim must pass. **Established**: the claim passed every gate. **Waiting**: a gate is due. **Stopped**: a gate said no, or could not run. **Unchecked**: the next gate is a command and the claim named nothing to run it on.
- **Score**: 1 to 5, how many rounds a frame is worth; 3 if nobody said. **Set aside**: its rounds are spent and it is still open.
- **Assumption**: something a round decided in the person's place.

## See what is going on

Read `GET /api/status?project=ID` first. It is plain text: whether the loop is running and on what, what it has spent, how far each claim got, what is queued, what was assumed, the latest handoff, and the calls you can make. Without `project` it lists the projects. If several fit and the context does not say which, ask.

When the digest is not enough: `GET /api/research?project=ID` returns every frame and claim with where it stands (`bucket`, `state`, `at`, each gate's verdict), and `GET /api/events?project=ID&after=N` returns the log. The page for a project is `/#/<project>`.

Report in the record's own terms.

- A result is **established** only when its `state` is `established`. Say which gates it passed, and which kind each was: a refuter that could not kill a claim is weaker than a command that passed.
- Anything else is still a claim. Say which gate it is waiting at or stopped at (`at`) and what the gate said.
- Nothing else in Hades checks a result. Do not call a claim true because a round said so.

## Explain, when the person asks

When the person asks to understand something (how the target works, what the code looks like, what a result means, why a gate said no), answer them, and save the explanation as a write-up so they can read it on the page and share it.

- A write-up is a Markdown file in `projects/<id>/writeups/` next to `server.py` (the digest gives the full path and lists the ones that exist). The file name is lower-case words joined by dashes and ends in `.md`; its first heading is its title.
- Revise the file that already covers the subject before adding another. One subject per file.
- Write it for someone who has not seen the project: start from what the thing is, show the real code or input you are describing with its file and line, and say plainly what is established, what is only a claim, and what you did not check.
- A write-up is your explanation, not the record. No round reads it and no gate checks it, so never put a result in a write-up that the record does not hold.

Its page link is `/#/<project>/writeups/<file name without .md>`. Give the person that link in your reply.

## Steer, when the person asks

The loop never waits for any of this. Everything is JSON POSTed to the server.

| To | Send |
|---|---|
| Queue places to look | `/api/frames` `{"project", "frames": ["text", {"text", "score", "area", "next"}]}` |
| Put a frame first, with its rounds to spend again | `/api/events` `{"project", "type": "research", "id", "priority": true, "source": "you"}` |
| Say how many rounds a frame is worth | the same, with `"score": 4` |
| Drop a frame or claim, rule it out, or reopen it | the same, with `"status": "superseded"`, `"contradicted"` or `"open"` |
| Overrule an assumption | `/api/events` `{"project", "type": "question", "id", "answer": "...", "source": "you"}` |
| Tell every round something | `/api/project` `{"project", "direction": "..."}` (`""` removes it) |
| Change the goal, target or rules | `/api/project` `{"project", "goal": "..."}` |
| Hold the loop before its next round, or release it | `/api/project` `{"project", "paused": true}` |
| Record that the person checked a claim themselves | `/api/events` `{..., "id", "audited": true, "source": "you"}`, only when they say they did |

Use the person's own words for a direction or an answer, and tell them what is now set. Keep a direction to what the rounds should work on, not how: the brief is short on purpose.

## Gates

A project that sets no gates gets one built in: a fresh round is shown only the claim and told to refute it. It is the weakest kind.

For stronger checking, write the gates for the person. There is no API for this: edit `projects/<id>/project.json` next to `server.py` and put each gate's script in that folder. The file is read fresh every time. Listing any `gates` replaces the built-in one.

```json
"artifact": "the path of a file holding the input that triggers the fault",
"lock": ["scope.md", "replay.py"],
"gates": [
  {"name": "reproduces", "command": "python3 {project}/replay.py"},
  {"name": "not reported", "recipe": "reader", "refute": "Look for an earlier report of this exact fault. Cite what you opened."},
  {"name": "matters", "rate": "How much would a maintainer of this code care, and why?"}
]
```

- A **command** gate is run with the claim's artifact as its last argument, in the folder the claim's round worked in; exit code 0 passes.
- A **refuter** (`refute`) is a fresh round that sees only the claim. A **rater** (`rate`) gives an established claim a number from 1 to 5 and never stops one. Either can name a `recipe` from `launchers.json` to run on another agent.
- Each refuter or rater costs a whole model round per claim. Make a check a command whenever a program can decide it, and among gates of the same cost put the one that rejects most first.
- `lock` names files in the project folder that define the question. Change one and every verdict stops counting; reword a gate and its own verdicts do.
- Test a gate on something that should pass and something that should not, and show the person what you wrote. Never loosen or remove a gate unless they ask.

## Run

`POST /api/launch {"project": "<id>"}` starts the loop with the saved settings and `POST /api/stop {"project": "<id>"}` stops it. Starting a loop spends the person's model usage, so say what you are about to start and wait for a yes.

To change how it runs, read `GET /api/project?project=<id>`, change the keys asked for, and send the whole `loop` object back with `POST /api/project {"project", "loop": {...}}`. The keys: `recipe`, `model`, `rounds` (0 = until stopped), `timeout_min` (0 = no limit), `workers` (1 to 4 loops side by side, each on its own frame), `spend` (the most model rounds one launch may use, research and checking together; 0 = no limit) and `keep_background`. Hades has no clock: to start a run at a set time, schedule the launch call yourself.

`POST /api/new {"project", "goal", "target", "agent_files"}` creates a project. A project id that does not exist is refused everywhere else.

Recipes live in `launchers.json` next to `server.py` (start from `launchers.example.json`): each has `command`, the argument list run each round with the brief on standard input (or `{brief_file}` / `{brief}` in an argument), and optionally `label`, `models`, `rounds` and `timeout_min`. The file is read at every launch.
