from pathlib import Path

from todo.store import Store, Todo


def test_add_assigns_increasing_ids(tmp_path: Path) -> None:
    store = Store(tmp_path / "todos.json")
    assert store.add("a") == Todo(id=1, text="a")
    assert store.add("b") == Todo(id=2, text="b")


def test_all_round_trips_through_the_file(tmp_path: Path) -> None:
    path = tmp_path / "todos.json"
    Store(path).add("a")
    assert Store(path).all() == [Todo(id=1, text="a")]


def test_empty_store(tmp_path: Path) -> None:
    assert Store(tmp_path / "missing.json").all() == []
