"""The Git view's GET routes as plain functions: the server passes the path after
/api/git/ and the parsed query, and sends back the (code, body) they return.
POST routes are answered by git_post after check_git_body."""

from pathlib import Path

from harness.ui.git_cmd import NoRepoError
from harness.ui.git_diff import read_commit, read_compare, read_file_diff
from harness.ui.git_read import read_branches, read_graph
from harness.ui.git_remote import fetch, push
from harness.ui.git_status import read_status
from harness.ui.git_write import (
    commit,
    create_branch,
    delete_branch,
    merge_branch,
    stage,
    switch_branch,
    unstage,
)
from harness.ui.gitops import GitOpError

LIMIT_MAX = 5000
GIT_POST_ROUTES = (
    "stage",
    "unstage",
    "commit",
    "branch",
    "switch",
    "merge",
    "delete-branch",
    "fetch",
    "push",
)
NETWORK_ROUTES = ("fetch", "push")
PATHS_MAX = 500
MESSAGE_MAX = 5000
FIELD_MAX = 200

_KEYS = {
    "stage": ("paths",),
    "unstage": ("paths",),
    "commit": ("message",),
    "branch": ("name", "start"),
    "switch": ("name",),
    "merge": ("name",),
    "delete-branch": ("name",),
    "fetch": ("remote",),
    "push": ("remote", "branch"),
}


class BadGitBody(ValueError):
    """A POST body that does not fit its /api/git/ route. The server answers 400 with it."""


def git_get(repo: Path, route: str, query: dict[str, list[str]]) -> tuple[int, dict]:
    """Answer GET /api/git/<route>?<query> for the project at `repo`."""

    def first(name: str) -> str:
        return query.get(name, [""])[0]

    def flag(name: str) -> bool:
        return first(name) == "1"

    try:
        if route == "graph":
            limit = first("limit") or "500"
            if not (limit.isascii() and limit.isdigit() and 1 <= int(limit) <= LIMIT_MAX):
                return 400, {"error": f"limit must be a whole number from 1 to {LIMIT_MAX}"}
            return 200, read_graph(repo, int(limit), flag("harness")).model_dump(mode="json")
        if route == "branches":
            return 200, read_branches(repo, flag("harness")).model_dump(mode="json")
        if route == "status":
            return 200, read_status(repo).model_dump(mode="json")
        if route == "compare":
            base, head = first("base"), first("head")
            if not base or not head:
                return 400, {"error": "send base and head"}
            return 200, read_compare(repo, base, head).model_dump(mode="json")
        if route == "diff":
            path = first("path")
            if not path:
                return 400, {"error": "send path"}
            return 200, read_file_diff(repo, path, flag("staged")).model_dump(mode="json")
        if route.startswith("commit/"):
            rev = route[len("commit/") :]
            if not rev:
                return 400, {"error": "send a commit"}
            return 200, read_commit(repo, rev).model_dump(mode="json")
        return 404, {"error": "not found"}
    except NoRepoError as err:
        return 409, {"error": str(err)}
    except GitOpError as err:
        return 400, {"error": str(err)}


def _text_ok(value: object, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def check_git_body(route: str, body: object) -> dict:
    """The body of a POST to /api/git/<route> (one of GIT_POST_ROUTES), or BadGitBody.
    Exactly the allowed keys: stage and unstage {paths}; commit {message}; branch
    {name, start}; switch, merge and delete-branch {name}; fetch {remote};
    push {remote, branch}."""
    if not isinstance(body, dict):
        raise BadGitBody("body must be a JSON object")
    keys = _KEYS[route]
    if set(body) != set(keys):
        raise BadGitBody(f"send exactly: {', '.join(keys)}")
    for key in keys:
        value = body[key]
        if key == "paths":
            if not (
                isinstance(value, list)
                and 1 <= len(value) <= PATHS_MAX
                and all(isinstance(p, str) and p for p in value)
            ):
                raise BadGitBody(f"paths must be a list of 1 to {PATHS_MAX} file paths")
        elif key == "message":
            if not _text_ok(value, MESSAGE_MAX):
                raise BadGitBody(
                    f"message must be a non-empty string of at most {MESSAGE_MAX} characters"
                )
        elif not _text_ok(value, FIELD_MAX):
            raise BadGitBody(f"{key} must be a non-empty string of at most {FIELD_MAX} characters")
    return body


def git_post(repo: Path, route: str, body: dict, busy: str | None) -> tuple[int, dict]:
    """Answer POST /api/git/<route> for the project at `repo`; `body` already passed
    check_git_body. `busy` is why writes are refused now, or None."""
    if route not in GIT_POST_ROUTES:
        return 404, {"error": "not found"}
    if busy is not None and route not in NETWORK_ROUTES:
        return 409, {"error": busy}
    try:
        if route == "stage":
            stage(repo, body["paths"])
        elif route == "unstage":
            unstage(repo, body["paths"])
        elif route == "commit":
            return 200, {"ok": True, "sha": commit(repo, body["message"])}
        elif route == "branch":
            create_branch(repo, body["name"], body["start"])
        elif route == "switch":
            switch_branch(repo, body["name"])
        elif route == "merge":
            return 200, {"ok": True, "sha": merge_branch(repo, body["name"])}
        elif route == "delete-branch":
            delete_branch(repo, body["name"])
        elif route == "fetch":
            return 200, {"ok": True, "output": fetch(repo, body["remote"])}
        else:
            return 200, {"ok": True, "output": push(repo, body["remote"], body["branch"])}
        return 200, {"ok": True}
    except GitOpError as err:
        return 409, {"error": str(err)}
