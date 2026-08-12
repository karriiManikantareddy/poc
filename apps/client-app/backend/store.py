"""JSON-file persistence for the Agent Config Store.

Stand-in for the Delta table / Lakebase store shown in architecture.drawio
page 3 ("Agent Config Store") — same role (the interpreter reads an agent's
own config from here, so editing an agent never requires a redeploy), just
backed by flat files instead of Lakebase for this POC.
"""
import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"


def _path(collection: str) -> Path:
    return DATA_DIR / f"{collection}.json"


def read(collection: str) -> list[dict[str, Any]]:
    path = _path(collection)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def write(collection: str, items: list[dict[str, Any]]) -> None:
    _path(collection).write_text(json.dumps(items, indent=2), encoding="utf-8")


def get_item(collection: str, item_id: str) -> dict[str, Any] | None:
    for item in read(collection):
        if item.get("id") == item_id:
            return item
    return None


def upsert_item(collection: str, item: dict[str, Any]) -> dict[str, Any]:
    items = read(collection)
    for i, existing in enumerate(items):
        if existing.get("id") == item["id"]:
            items[i] = item
            write(collection, items)
            return item
    items.append(item)
    write(collection, items)
    return item


def delete_item(collection: str, item_id: str) -> bool:
    items = read(collection)
    remaining = [i for i in items if i.get("id") != item_id]
    if len(remaining) == len(items):
        return False
    write(collection, remaining)
    return True


def append_item(collection: str, item: dict[str, Any]) -> dict[str, Any]:
    items = read(collection)
    items.append(item)
    write(collection, items)
    return item
