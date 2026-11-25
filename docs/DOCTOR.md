# `harness doctor`

Checks the environment before a run: `harness doctor [--repo PATH]`.

Each check prints `OK <name>` or `FAIL <name> -- fix: <one-line fix>`. The command exits 1 if
any check fails, 0 otherwise.

Checks:

1. `git` is on the PATH.
2. `uv` is on the PATH.
3. The repo is a git repo.
4. The working tree is clean (`git status --porcelain` is empty).
5. The planner skills (`plan-project`, `plan-stage`) exist under `.claude/skills/`.
6. `.harness/` is git-excluded (`git check-ignore`).

The checks live in `src/harness/doctor.py`; tests are in `tests/test_doctor.py`.
