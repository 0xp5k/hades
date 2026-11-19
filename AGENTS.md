# Project reporting

When the user is working on a Hades project, use the running dashboard as the shared project record. Use the server and project from the dashboard URL or workspace context. The local server defaults to http://127.0.0.1:8765. List projects with GET /api/projects. If only one project exists, use it. If several projects fit and the context does not identify one, ask which project the user means. Read its configuration and latest updates before reporting what happened.

When the evidence or the user's direction changes, update the existing project brief. Read the project events with GET /api/events?project=ID and use the after offset for additional pages. Preserve the current note key in section `now`, or use `Current update` on a new blank brief. POST /api/events with the project ID, type `item`, section `now`, key and complete Markdown body. Check the saved version. Keep evidence references and open questions where the next reader can find them.

When the user asks to understand something about the project, publish or revise the explanation in its Library as part of answering. Check the project's configured note sections and its latest `topic` events for an existing page about the subject, even if the user phrased it differently.

- For an existing topic, reuse its exact `key`. For a new topic, choose a short reader-facing title. POST JSON to `/api/events` with `project`, `type` set to `topic`, `key`, your `agent` name and the complete Markdown explanation in `body`. A topic appears immediately in the Library in every project phase. Its link is `/#/<project>/t.<title>` with each path component URL-encoded.
- If the explanation already lives in a configured note section, revise its existing `item` events by section ID and item key. Its link stays `/#/<project>/s.<section-id>`.
- If a grid card introduces the subject, preserve its content and set its `data.page` to `t.<title>` for a topic or the section ID for a configured note.

Read back the saved content and include the page link in your reply.

On the Experiments page, an item status describes the reporting agent’s assessment. Cite the verifier record and its build and configuration before describing RCE as confirmed. An edit creates a new experiment revision.
