"""JSON-file storage for todos."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Todo:
    id: int
    text: str
    done: bool = False


class Store:
    """Todos in one JSON file: {"todos": [{"id": 1, "text": "...", "done": false}, ...]}."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> list[Todo]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [Todo(**item) for item in data["todos"]]

    def _save(self, todos: list[Todo]) -> None:
        data = {"todos": [asdict(t) for t in todos]}
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def add(self, text: str) -> Todo:
        """Append a todo with the next id (1 for an empty store) and save it."""
        todos = self._load()
        todo = Todo(id=max((t.id for t in todos), default=0) + 1, text=text)
        todos.append(todo)
        self._save(todos)
        return todo

    def all(self) -> list[Todo]:
        """Every todo, in id order."""
        return sorted(self._load(), key=lambda t: t.id)
