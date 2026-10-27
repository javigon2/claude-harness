# harness

A plan-and-execute harness for Claude agents. It splits a goal into work orders, runs agents on them, and checks the results. The office page shows the run and lets you talk to the agents.

Install dependencies with `uv sync`. Then run `uv run harness ui` to open the office page. Add `--fake` to use scripted agents. Fake mode makes no model calls.

## Office page: images and diagrams

### Images

You can attach images to a message in three ways. Use the Attach image button, drag and drop a file onto a message box, or paste an image.

Attachments work in the Orchestrator chat, in the Opus chat (prompts, answers, and change requests), and in agent chat boxes.

Limits:

- At most 4 images per message.
- At most 8 MB per image.
- Only PNG, JPEG, GIF, and WebP files are accepted. The type is checked by content. SVG files are refused.
- A message still needs text. An image alone is not enough.

Where images go:

- Each image is stored once per content in `.harness/uploads/<sha256>.<ext>`.
- Images are never deleted automatically. Remove files from that folder yourself if you need the space.

How agents see images:

- Agents receive `Attached images: <absolute paths>` after the message text.
- Agents open the files with their Read tool.

Routes:

- `POST /api/upload` takes the raw image bytes. It needs a token. A read-only server refuses it.
- `GET /api/upload/<id>` returns a stored image.

If the browser has no file support, the attach controls stay hidden.

### Diagrams

Not yet available.
