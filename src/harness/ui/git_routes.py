"""The Git view's GET routes as plain functions: the server passes the path after
/api/git/ and the parsed query, and sends back the (code, body) they return."""

from pathlib import Path

from harness.ui.git_cmd import NoRepoError
from harness.ui.git_diff import read_commit, read_compare, read_file_diff
from harness.ui.git_read import read_branches, read_graph
from harness.ui.git_status import read_status
from harness.ui.gitops import GitOpError

LIMIT_MAX = 5000


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
