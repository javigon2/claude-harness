Extend the `todo` CLI in this repo (Python 3.12, uv, typer, pytest, ruff; todos in a JSON
file) into a small but complete todo manager. Add: `todo done <id>` to mark a todo done;
`todo remove <id>` to delete one; an optional priority (`low`, `normal`, `high`, default
`normal`) set with `todo add --priority high "text"` and shown by `list`; `todo list
--pending` to hide done todos; and `todo search <word>` for a case-insensitive text search.
Unknown ids exit with code 1 and a clear message on stderr. Existing todos files without a
priority must keep loading. Every command has tests.
