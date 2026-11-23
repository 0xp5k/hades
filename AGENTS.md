# Working with Hades

Hades runs a research loop and keeps its record. This file is for an assistant the person is talking to: how to see what the loop is doing, explain it, and steer it for them. The server is local and defaults to http://127.0.0.1:8765; use the address from the page or the context if it differs.

**If you are a round inside the loop** (you were started with a brief that ends in a Handoff block), none of this is for you. Do what the brief says, end with the handoff, and do not call the server.

## The words

- **Thread**: one line of the record, with an id like `k12`. It is one of the next three things.
- **Frame**: a place to look, not yet settled. Leads from a round become frames; you can queue them.
- **Claim**: what a round says it found when it settles a frame. **Ruled out**: the frame was examined and nothing was there.
- **Gate**: a test a claim must pass. **Established**: the claim passed every gate. **Waiting**: a gate is due and runs after the next round. **Stopped**: a gate said no, or could not run. **Unchecked**: the next gate is a command and the claim named nothing to run it on.
- **Score**: 1 to 5, how many rounds a frame is worth; 3 if nobody said. A round spends one when it starts on the frame. **Set aside**: the rounds are spent and the frame is still open; it leaves the queue.
- **Assumption**: something a round decided in the person's place.

## See what is going on

Read `GET /api/status?project=ID` first. It is plain text: whether the loop is running and on what, how far each claim got, what is queued, what a round assumed in the person's place, and the latest handoff. Without `project` it lists the projects. If several fit and the context does not say which, ask.

When the digest is not enough: `GET /api/research?project=ID` returns every thread with where it stands, and `GET /api/events?project=ID&after=N` returns the log, 5,000 events at a time. A thread's page link is `/#/<project>/research`.

Report in the record's own terms.

- A result is **established** only when the thread's `state` is `established`: it passed every one of the project's gates. Say which gates. A gate that is a refuter (a fresh round that tried to kill the claim and could not) is weaker than a command that passed; say which kind it was.
- Any other `state` means it is still a claim. Say which gate it is waiting at or stopped at (`at`) and what the gate said.
- An **assumption in force** is something a round decided in the person's place because nobody was there to ask.
- Nothing else in Hades checks a result. Do not describe a claim as true because a round said so.

## Explain

When the person asks to understand something about the project, publish or revise the explanation in its Library as part of answering. Check the latest `topic` events and the project's note sections for an existing page about the subject, even if the person phrased it differently.

- For an existing topic, reuse its exact `key`. For a new one, choose a short reader-facing title. POST JSON to `/api/events` with `project`, `type` set to `topic`, `key`, your `agent` name and the complete Markdown explanation in `body`. Its link is `/#/<project>/t.<title>` with each path component URL-encoded.
- If the explanation already lives in a note section, revise its `item` events by section ID and item key. Its link stays `/#/<project>/s.<section-id>`.

Read back the saved content and include the page link in your reply.

## Steer, when the person asks

The loop never waits for any of this. It works the best open frame, and when none is left it widens and finds more. Everything here changes what it does next; nothing here is needed for it to carry on. All of it is JSON POSTed to the server.

| To | Send |
|---|---|
| Queue places to look | `/api/frames` `{"project", "frames": ["text", {"text", "score", "area", "next"}]}` |
| Put a thread at the front, with its rounds to spend again | `/api/events` `{"project", "type": "research", "id", "priority": true, "source": "you"}` |
| Say how many rounds a frame is worth (1 to 5) | the same, with `"score": 4` |
| Drop a thread, or reopen it | the same, with `"status": "superseded"` or `"open"` (`"contradicted"` rules it out; no other status can be set this way) |
| Overrule an assumption | `/api/events` `{"project", "type": "question", "id", "answer": "...", "source": "you"}` |
| Tell every round something | `/api/project` `{"project", "direction": "..."}` (`""` removes it) |
| Change the goal, target or rules | `/api/project` `{"project", "goal": "..."}` |
| Hold the loop before its next round, or release it | `/api/project` `{"project", "paused": true}` |
| Record that the person checked a claim themselves | `/api/events` `{..., "id", "audited": true, "source": "you"}`, only when they say they did |

Use the person's own words for a direction or an answer. Tell them what is now set.

## Gates

A gate is a test a claim must pass before it counts. A project that sets none gets one built in: after each round, a fresh round is shown only the claim and told to refute it. That needs no setup, and it is the weakest kind.

When the person wants stronger checking, write the gates for them. There is no API for this: edit the file `projects/<id>/project.json` next to `server.py`, and put each gate's script in that same folder. The server reads the file fresh every time, so the change is live at once. Listing any `gates` replaces the built-in one, so add a `refute` gate of your own if you still want a refuter. If the file stops being valid JSON the loop holds and the digest says so.

```json
"artifact": "the path of a file holding the input that triggers the fault",
"lock": ["scope.md", "replay.py"],
"gates": [
  {"name": "reproduces", "command": "python3 {project}/replay.py"},
  {"name": "not known", "refute": "Look for an earlier report of this exact fault. It is refuted only if you can cite one."}
]
```

A gate with `"rate"` in place of `"refute"` is a rater: it never stops a claim and runs after everything else, it gives an established one a number from 1 to 5 for how much it matters (`rating` on the thread), and results are listed in that order. A refuter or rater can add `"recipe": "<name>"` to run on a different agent from `launchers.json`, which is how to get a second model's check or a refuter with web access.

A command gate is run with the claim's artifact as its last argument, and exit code 0 passes. It runs in the folder the claim's round worked in (a launched loop works in `agent-work/<id>/<recipe>-1/`), so a relative artifact path is found there. A claim that names no artifact cannot be run through a command gate and stays unchecked until a round claims it again with one. Order them cheapest first: it is true, it is the thing asked, it is new, it is usable. `lock` names files in that folder that define the question; when they change every earlier verdict stops counting, and when one gate changes its own verdicts do, and those gates run again. Among gates of the same cost, put the one that rejects most first. Test a gate on something that should pass and something that should not before you leave it in place, and show the person what you wrote. Never loosen or remove a gate unless they ask.

A round inside the loop never touches gates, never reports a verdict, and never sets `audited`.

## Run

A project runs one launch at a time. `POST /api/launch {"project": "<id>"}` starts it with the saved settings and `POST /api/stop {"project": "<id>"}` stops it. Starting a loop spends the person's model usage, so say what you are about to start and wait for a yes.

To change how it runs, read `GET /api/project?project=<id>`, change the keys asked for, and send the whole `loop` object back with `POST /api/project {"project", "loop": {...}}`; a key you leave out returns to the recipe's own value. The keys: `recipe`, `model`, `rounds` (0 = until stopped), `timeout_min` (0 = no limit), `reply_wait` (seconds; 0 = never), `keep_background`, `workers` (1 to 4 rounds side by side, each on its own frame; each spends its own usage), and `tools` (`recipe`, or `wide` to add web search, web fetch and subagents).

`POST /api/new {"project", "goal", "target", "agent_files"}` creates a project: `goal` is what to research, `target` (optional) says what is being studied and where it lives, and `agent_files` (optional) is a folder whose contents the loop's working folder starts with. A project id that does not exist is refused everywhere else, so a typo never starts a second project.

## Changing the harness

Launch recipes live in `launchers.json` next to `server.py` (start from `launchers.example.json`). Each key is a recipe name; its fields:

- `command`: the argv list the loop runs each round, with the brief on standard input (or `{brief_file}` / `{brief}` in an argument).
- `label`: the name the page shows. `models`: the model names offered at launch; the chosen one replaces the value after `--model`, `-m` or `--model=`.
- `interactive`: `false` keeps a direct `claude -p` recipe as a one-way batch run. `rounds`: the default number of rounds. `timeout_min`: the limit for one round.

A recipe may be a wrapper script that pairs Claude with other models or tools. Forward the wrapper's final batch call through the capture helper (see Run details in the guide) so the operator sees the real command and input. The file is read at every launch: an edit takes effect on the next launch, with no restart.
