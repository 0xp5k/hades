# Project reporting

When the user is working on a Hades project, use the running dashboard as the shared project record. Use the server and project from the dashboard URL or workspace context. The local server defaults to http://127.0.0.1:8765. List projects with GET /api/projects. If only one project exists, use it. If several projects fit and the context does not identify one, ask which project the user means. Read its configuration and latest updates before reporting what happened.

When the evidence or the user's direction changes, update the existing project brief. Read the project events with GET /api/events?project=ID and use the after offset for additional pages. Preserve the current note key in section `now`, or use `Current update` on a new blank brief. POST /api/events with the project ID, type `item`, section `now`, key and complete Markdown body. Check the saved version. Keep evidence references and open questions where the next reader can find them.

When the user asks to understand something about the project, publish or revise the explanation in its Library as part of answering. Check the project's configured note sections and its latest `topic` events for an existing page about the subject, even if the user phrased it differently.

- For an existing topic, reuse its exact `key`. For a new topic, choose a short reader-facing title. POST JSON to `/api/events` with `project`, `type` set to `topic`, `key`, your `agent` name and the complete Markdown explanation in `body`. A topic appears immediately in the Library in every project phase. Its link is `/#/<project>/t.<title>` with each path component URL-encoded.
- If the explanation already lives in a configured note section, revise its existing `item` events by section ID and item key. Its link stays `/#/<project>/s.<section-id>`.
- If a grid card introduces the subject, preserve its content and set its `data.page` to `t.<title>` for a topic or the section ID for a configured note.

Read back the saved content and include the page link in your reply.

The project's `direction`, its loop settings, `simple` and the `priority` mark on research threads are the person's. A worker running inside a loop follows them and never sets or clears them. Never edit files listed in the project's `protected` patterns.

## Changing settings for the person

When the person is talking with you and asks to change how the loop runs (the model, rounds, time limit, round style, number of workers, tools, brief style or their standing direction), make the change for them:

```sh
curl -s -X POST http://127.0.0.1:8765/api/coordinate -H 'Content-Type: application/json' \
  -d '{"tool": "loop_settings", "args": {"project": "<id>", "model": "fable", "timeout_min": 0, "style": "explore"}}'
```

Only the keys you send change, `null` clears one back to the recipe's own value, and sending just the project returns what is set now. The keys: `recipe`, `model`, `rounds` (0 = until stopped), `timeout_min` (0 = no limit), `reply_wait` (seconds; 0 = never), `keep_background`, `style` (`focused` or `explore`), `agents` (1 to 5), `tools` (`recipe`, or `wide` to add web search, web fetch and subagents), `direction` and `simple`. Tell the person what is now set. Change only what they asked for. Starting a loop spends their model usage, so say what you are about to start and wait for a yes.

On the Experiments page, an item status describes the reporting agent’s assessment. Cite the verifier record and its build and configuration before describing a result as confirmed. An edit creates a new experiment revision.

For the Evidence view, use an existing component name in `data.component` on experiments or in `area` on findings and research notes. Add a saved evidence reference when one is available. Review priority reflects gaps in the evidence and verifier coverage.

When creating a one-off wrapper that changes the CLI prompt, forward its final batch invocation through the Run details capture helper described in the guide. This lets the operator inspect the actual command and input, including wrapper edits.

## Changing the harness

Launch recipes live in `launchers.json` next to `server.py` (start from `launchers.example.json`). Each key is a recipe name; its fields:

- `command`: the argv list the loop runs each round, with the brief on standard input (or `{brief_file}` / `{brief}` in an argument).
- `label`: the name the page shows. `models`: the model names offered at launch; the chosen one replaces the value after `--model`, `-m` or `--model=`.
- `interactive`: `false` keeps a direct `claude -p` recipe as a one-way batch run. `rounds`: the default number of rounds. `timeout_min`: the limit for one round.

A recipe may be a wrapper script that pairs Claude with other models or tools. Forward the wrapper's final batch call through the capture helper (see Run details in the guide) so the operator sees the real command and input. The file is read at every launch: an edit takes effect on the next launch, with no restart.
