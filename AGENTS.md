# Project reporting

When the user is working on a Hades project, use the running dashboard as the shared project record. List projects with GET /api/projects on the local server at http://127.0.0.1:8765. Use the project selected by the user. Read its configuration and latest updates before reporting what happened.

When the evidence or the user's direction changes, update the existing project brief. Read the project events with GET /api/events?project=ID and use the after offset for additional pages. Preserve the current note key in section `now`, or use `Current update` on a new blank brief. POST /api/events with the project ID, type `item`, section `now`, key and complete Markdown body. Check the saved version. Keep evidence references and open questions where the next reader can find them.
