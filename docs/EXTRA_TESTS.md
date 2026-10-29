# Extra tests

Four supplementary test files extend coverage of the titles, worktree, state and events modules, and each can be run on its own. The full suite is run on demand from the page's Run tests button.

## tests/test_titles_extra.py

Covers `src/harness/titles.py`: title cleaning and fallback edge cases, reading and writing titles, extracting request text, and the Haiku title helper (using a fake query function). Also checks the `ensure_title` / `ensure_request_title` fallbacks and that they never raise.

- test_clean_title_edge_cases
- test_fallback_title_edge_cases
- test_write_read_titles
- test_request_text_blank_user_falls_back_to_description
- test_request_text_is_truncated
- test_request_text_later_session_wins
- test_request_text_without_log
- test_haiku_title_result_wins
- test_haiku_title_ignores_subagent
- test_haiku_title_options
- test_ensure_request_title_long_text_and_empty_result
- test_ensure_request_title_model_reply_cleans_to_empty
- test_ensure_never_raises_on_bad_repo

```bash
uv run pytest tests/test_titles_extra.py -q
```

## tests/test_worktree_extra.py

Covers `src/harness/worktree.py` using real git repositories in `tmp_path`: branch and path naming, creating worktrees from a custom base or the integration branch, commit messages, merge conflicts leaving the branch untouched, `ensure_excluded` from inside a worktree, and cleanup edge cases.

- test_branch_and_path_naming
- test_create_with_custom_base
- test_default_base_is_integration_branch
- test_commit_message_and_noop
- test_merge_conflict_leaves_clean_branch
- test_ensure_excluded_idempotent_from_worktree
- test_cleanup_empty_run_does_not_raise
- test_cleanup_leaves_other_run_branch

```bash
uv run pytest tests/test_worktree_extra.py -q
```

## tests/test_state_extra.py

Covers `src/harness/state.py`: `new_run_id` with a non-UTC timezone, pid round trip, save/load round trip and overwrite, rejection of unknown fields, and `latest_run` ignoring directories without `state.json` and plain files.

- test_new_run_id_converts_offset_time_to_utc
- test_pid_round_trips
- test_save_twice_overwrites
- test_unknown_field_is_rejected
- test_latest_run_ignores_dirs_without_state_and_plain_files

```bash
uv run pytest tests/test_state_extra.py -q
```

## tests/test_events_extra.py

Covers `src/harness/events.py`: `emit` defaults, `read_events` skipping blank lines, and `follow()` skipping blank lines, holding partial lines until complete, and waiting for a missing file.

- test_emit_defaults
- test_read_events_skips_blank_lines
- test_follow_skips_blank_and_holds_partial_line
- test_follow_waits_for_missing_file

```bash
uv run pytest tests/test_events_extra.py -q
```

## All four files

```bash
uv run pytest tests/test_titles_extra.py tests/test_worktree_extra.py tests/test_state_extra.py tests/test_events_extra.py -q
```
