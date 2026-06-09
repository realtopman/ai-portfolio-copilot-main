"""
Sync Monday.com epics, backlog items, and subitems into timeBuzzer tiles.

The CLI defaults to a dry run:
  python -m services.timebuzzer

Create missing tiles:
  python -m services.timebuzzer --execute

Required environment variables:
  MONDAY_API_TOKEN
  TIMEBUZZER_API_KEY

Layer IDs can be supplied with either:
  TIMEBUZZER_LAYER_IDS=123,456,789
or:
  TIMEBUZZER_EPIC_LAYER_ID=123
  TIMEBUZZER_ITEM_LAYER_ID=456
  TIMEBUZZER_SUBITEM_LAYER_ID=789
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from config import Config
from services.monday_api import MondayAPI

TIMEBUZZER_BASE_URL = Config.TIMEBUZZER_BASE_URL
DEFAULT_WORKSPACE_ID = Config.WORKSPACE_ID_2
TIMEBUZZER_TILE_FIELDS = (
    "name",
    "children",
    "parents",
    "archived",
    "type",
    "layer",
    "customData",
    "favorite",
    "color",
    "description",
)

EPIC_BOARD_NAMES = ("Epic", "Epics")
SPRINT_BACKLOG_BOARD_NAMES = ("Sprint Backlog", "Sprint backlog")
SPRINTS_BOARD_NAMES = ("Sprints", "Sprint")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LayerIds:
    epic: int
    item: int
    subitem: int


@dataclass
class SprintRef:
    key: str
    name: str = ""


def load_env_file(path: str = ".env") -> None:
    """Load a local .env without requiring python-dotenv at import time."""
    try:
        from dotenv import load_dotenv

        load_dotenv(path)
        return
    except ImportError:
        pass

    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def normalize_timebuzzer_authorization(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    value = value.strip()
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if value.lower().startswith(("apikey ", "bearer ")):
        return value
    return f"APIKey {value}" if value else None


def normalize_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def parse_date(value: Any) -> Optional[datetime]:
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None

    if not parsed:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if not parsed:
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def compact_text(*parts: Any) -> str:
    return "\n".join(str(part).strip() for part in parts if str(part or "").strip())


def trim_name(value: Any, fallback: str) -> str:
    text = " ".join(str(value or "").split())
    return (text or fallback)[:255]


class TimeBuzzerClient:
    def __init__(self, api_key: str, base_url: str = TIMEBUZZER_BASE_URL):
        authorization = normalize_timebuzzer_authorization(api_key)
        if not authorization:
            raise RuntimeError("TIMEBUZZER_API_KEY is missing.")

        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": authorization,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(
            method=method,
            url=f"{self.base_url}/{path.lstrip('/')}",
            timeout=60,
            **kwargs,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} failed: {response.status_code} {response.text[:1000]}")
        if not response.content:
            return None
        return response.json()

    def get_tiles(self, archived: Optional[bool] = False) -> List[Dict[str, Any]]:
        params: Dict[str, str] = {}
        if archived is not None:
            params["archived"] = "true" if archived else "false"
        data = self.request("GET", "/tiles", params=params)
        return extract_list_payload(data)

    def get_all_tiles(self) -> List[Dict[str, Any]]:
        by_id: Dict[str, Dict[str, Any]] = {}
        for archived in (False, True):
            for tile in self.get_tiles(archived=archived):
                tile_id = str(tile.get("id") or "")
                if tile_id:
                    by_id[tile_id] = tile
        return list(by_id.values())

    def filter_activities(self, filters: Dict[str, Any], offset: int = 0, count: int = 100) -> List[Dict[str, Any]]:
        data = self.request(
            "POST",
            "/activities/filters",
            params={"offset": str(offset), "count": str(count)},
            json=filters,
        )
        if isinstance(data, dict):
            for key in ("activities", "data", "items", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            return [data]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    def get_activity(self, activity_id: Any) -> Dict[str, Any]:
        data = self.request("GET", f"/activities/{activity_id}")
        if isinstance(data, dict):
            for key in ("activity", "data", "item", "result"):
                value = data.get(key)
                if isinstance(value, dict):
                    return value
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        raise RuntimeError(f"Unexpected timeBuzzer activity response: {data!r}")

    def create_tile(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = self.request("POST", "/tiles", json=payload)
        if isinstance(data, dict):
            return data
        raise RuntimeError(f"Unexpected timeBuzzer tile response: {data!r}")

    def delete_tile(self, tile_id: Any) -> Any:
        return self.request("DELETE", f"/tiles/{tile_id}")

    def update_tile(self, tile_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        last_error = None
        for method in ("PUT", "PATCH"):
            response = self.session.request(
                method=method,
                url=f"{self.base_url}/tiles/{tile_id}",
                timeout=60,
                json=payload,
            )
            if response.status_code in (404, 405):
                last_error = f"{method} /tiles/{tile_id} failed: {response.status_code} {response.text[:1000]}"
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"{method} /tiles/{tile_id} failed: {response.status_code} {response.text[:1000]}")
            if not response.content:
                return dict(payload)
            data = response.json()
            if isinstance(data, dict):
                return data
            raise RuntimeError(f"Unexpected timeBuzzer tile update response: {data!r}")
        raise RuntimeError(last_error or f"Could not update timeBuzzer tile {tile_id}.")


def extract_list_payload(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("tiles", "data", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [data]
    return []


def column_meta_by_id(board: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {column["id"]: column for column in board.get("columns", []) if isinstance(column, dict) and column.get("id")}


def column_title(meta: Dict[str, Dict[str, Any]], column_value: Dict[str, Any]) -> str:
    return str(meta.get(column_value.get("id"), {}).get("title") or column_value.get("id") or "")


def column_display_value(column_value: Dict[str, Any]) -> str:
    return str(
        column_value.get("display_value")
        or column_value.get("text")
        or parse_json_object(column_value.get("value")).get("text")
        or ""
    ).strip()


def extract_relation_refs(
    item: Dict[str, Any],
    meta: Dict[str, Dict[str, Any]],
    keyword: str,
) -> List[SprintRef]:
    refs: List[SprintRef] = []
    for column_value in item.get("column_values", []) or []:
        title = column_title(meta, column_value)
        label = f"{column_value.get('id', '')} {title}".lower()
        if keyword.lower() not in label:
            continue

        display = column_display_value(column_value)
        linked_ids = column_value.get("linked_item_ids") or []
        for linked_id in linked_ids:
            refs.append(SprintRef(key=f"id:{linked_id}", name=display))

        if display and not linked_ids:
            for name in display.split(","):
                normalized = normalize_name(name)
                if normalized:
                    refs.append(SprintRef(key=f"name:{normalized}", name=name.strip()))

    return refs


def item_sprint_refs(item: Dict[str, Any], backlog_meta: Dict[str, Dict[str, Any]]) -> List[SprintRef]:
    return extract_relation_refs(item, backlog_meta, "sprint")


def item_epic_refs(item: Dict[str, Any], backlog_meta: Dict[str, Dict[str, Any]]) -> List[SprintRef]:
    return extract_relation_refs(item, backlog_meta, "epic")


def latest_sprint_keys(
    sprint_board: Optional[Dict[str, Any]],
    sprint_items: List[Dict[str, Any]],
    backlog_items: List[Dict[str, Any]],
    backlog_meta: Dict[str, Dict[str, Any]],
    count: int,
) -> List[str]:
    if sprint_board and sprint_items:
        keys: List[str] = []
        for sprint in sprint_items[:count]:
            sprint_id = sprint.get("id")
            if sprint_id:
                keys.append(f"id:{sprint_id}")
            normalized = normalize_name(sprint.get("name"))
            if normalized:
                keys.append(f"name:{normalized}")
        return keys

    refs_by_key: Dict[str, Tuple[datetime, str]] = {}
    for item in backlog_items:
        item_date = parse_date(item.get("updated_at")) or datetime.min
        for ref in item_sprint_refs(item, backlog_meta):
            current = refs_by_key.get(ref.key)
            if not current or item_date > current[0]:
                refs_by_key[ref.key] = (item_date, ref.name)

    return [key for key, _ in sorted(refs_by_key.items(), key=lambda pair: pair[1][0], reverse=True)[:count]]


def filter_items_by_sprints(
    backlog_items: List[Dict[str, Any]],
    backlog_meta: Dict[str, Dict[str, Any]],
    sprint_keys: Iterable[str],
) -> List[Dict[str, Any]]:
    selected_keys = set(sprint_keys)
    selected = []
    for item in backlog_items:
        refs = item_sprint_refs(item, backlog_meta)
        if any(ref.key in selected_keys for ref in refs):
            selected.append(item)
    return selected


def parse_layer_ids(explicit: Optional[str] = None, existing_tiles: Optional[List[Dict[str, Any]]] = None) -> LayerIds:
    
    inferred = infer_layer_ids_from_existing_tiles(existing_tiles or [])
    if inferred:
        return inferred

    raise RuntimeError(
        "Could not determine timeBuzzer layer order from existing tile parents. Set TIMEBUZZER_LAYER_IDS "
        "as root_epic,item,subitem or set TIMEBUZZER_EPIC_LAYER_ID, TIMEBUZZER_ITEM_LAYER_ID, "
        "and TIMEBUZZER_SUBITEM_LAYER_ID."
    )


def as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def tile_parent_ids(tile: Dict[str, Any]) -> List[int]:
    parents = tile.get("parents") or []
    if isinstance(parents, dict):
        parents = [parents]
    if not isinstance(parents, list):
        return []

    parent_ids: List[int] = []
    for parent in parents:
        if isinstance(parent, dict):
            parent_id = as_int(parent.get("id"))
        else:
            parent_id = as_int(parent)
        if parent_id is not None:
            parent_ids.append(parent_id)
    return parent_ids


def infer_layer_ids_from_existing_tiles(tiles: List[Dict[str, Any]]) -> Optional[LayerIds]:
    tile_by_id = {str(tile.get("id")): tile for tile in tiles if tile.get("id") is not None}
    children_by_parent_layer: Dict[int, set[int]] = {}
    child_layers: set[int] = set()
    parent_layers: set[int] = set()

    for tile in tiles:
        child_layer = as_int(tile.get("layer"))
        if child_layer is None:
            continue

        for parent_id in tile_parent_ids(tile):
            parent_tile = tile_by_id.get(str(parent_id))
            parent_layer = as_int((parent_tile or {}).get("layer"))
            if parent_layer is None or parent_layer == child_layer:
                continue
            children_by_parent_layer.setdefault(parent_layer, set()).add(child_layer)
            parent_layers.add(parent_layer)
            child_layers.add(child_layer)

    roots = sorted(parent_layers - child_layers)
    for root_layer in roots:
        item_layers = sorted(children_by_parent_layer.get(root_layer, set()))
        for item_layer in item_layers:
            subitem_layers = sorted(children_by_parent_layer.get(item_layer, set()))
            for subitem_layer in subitem_layers:
                return LayerIds(epic=root_layer, item=item_layer, subitem=subitem_layer)

    return None


def existing_tiles_by_custom_data(tiles: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        str(tile.get("customData")): tile
        for tile in tiles
        if str(tile.get("customData") or "").startswith("monday:")
    }


def tile_name_layer_key(tile_or_payload: Dict[str, Any]) -> Tuple[int, str]:
    return as_int(tile_or_payload.get("layer")) or -1, normalize_name(tile_or_payload.get("name"))


def existing_tiles_by_name_layer(tiles: List[Dict[str, Any]]) -> Dict[Tuple[int, str], List[Dict[str, Any]]]:
    by_key: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    for tile in tiles:
        if tile.get("archived") is True:
            continue
        key = tile_name_layer_key(tile)
        if key[0] == -1 or not key[1]:
            continue
        by_key.setdefault(key, []).append(tile)
    return by_key


def choose_canonical_tile(candidates: List[Dict[str, Any]], custom_data: str) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None

    def sort_key(tile: Dict[str, Any]) -> Tuple[int, int, int]:
        tile_custom_data = str(tile.get("customData") or "")
        return (
            0 if tile_custom_data == custom_data else 1,
            0 if tile_custom_data.startswith("monday:") else 1,
            as_int(tile.get("id")) or 999999999,
        )

    return sorted(candidates, key=sort_key)[0]


def parents_equal(tile: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    tile_parents = sorted(tile_parent_ids(tile))
    payload_parents = sorted(as_int(parent) for parent in payload.get("parents") or [] if as_int(parent) is not None)
    return tile_parents == payload_parents


def tile_needs_update(tile: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    for key in ("name", "archived", "type", "layer", "customData", "favorite", "color", "description"):
        if tile.get(key) != payload.get(key):
            return True
    return not parents_equal(tile, payload)


def merge_update_payload(tile: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    merged = {key: tile.get(key) for key in TIMEBUZZER_TILE_FIELDS}
    merged.update({key: payload.get(key) for key in TIMEBUZZER_TILE_FIELDS if key in payload})
    merged["parents"] = payload.get("parents") if "parents" in payload else (tile_parent_ids(tile) or None)
    merged["children"] = payload.get("children") if "children" in payload else tile.get("children")
    return merged


def tile_id(tile: Dict[str, Any]) -> Optional[int]:
    value = tile.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def make_tile_payload(
    name: str,
    layer_id: int,
    custom_data: str,
    parents: Optional[List[int]] = None,
    color: str = "#0891B2FF",
    description: str = "",
) -> Dict[str, Any]:
    return {
        "name": trim_name(name, "Untitled"),
        "children": None,
        "parents": parents or None,
        "archived": False,
        "type": "Normal",
        "layer": layer_id,
        "customData": custom_data,
        "favorite": False,
        "color": color,
        "description": description,
    }


def ensure_tile(
    client: TimeBuzzerClient,
    payload: Dict[str, Any],
    existing_by_custom_data: Dict[str, Dict[str, Any]],
    existing_by_name_layer: Dict[Tuple[int, str], List[Dict[str, Any]]],
    execute: bool,
    created: List[Dict[str, Any]],
    updated: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
    archived_duplicates: List[Dict[str, Any]],
    kind: str,
    board: Dict[str, Any],
    monday_item: Dict[str, Any],
    progress: bool,
) -> Dict[str, Any]:
    custom_data = str(payload.get("customData") or "")
    existing = existing_by_custom_data.get(custom_data)
    if existing:
        if tile_needs_update(existing, payload):
            tile = update_existing_tile(client, existing, payload, execute, updated, kind, board, monday_item, progress)
            existing_by_custom_data[custom_data] = tile
            return tile
        skipped.append(existing)
        # print_progress("skip_existing", kind, payload, board, monday_item, existing, progress=progress)
        return existing

    if not execute:
        preview = dict(payload)
        preview["id"] = None
        created.append(preview)
        # print_progress("would_create", kind, payload, board, monday_item, preview, progress=progress)
        return preview

    # print_progress("create_start", kind, payload, board, monday_item, progress=progress)
    try:
        tile = client.create_tile(payload)
    except Exception as exc:
        print_progress("create_failed", kind, payload, board, monday_item, error=str(exc), progress=progress)
        if "needs a parent tile" in str(exc).lower():
            raise RuntimeError(
                f"{exc}\n"
                f"Layer/parent problem while creating {kind} {monday_item.get('id')} - {monday_item.get('name')!r}. "
                f"Check that layer {payload.get('layer')} is the correct {'root' if kind == 'epic' else 'child'} "
                "layer and that parent IDs are present."
            ) from exc
        raise
    existing_by_custom_data[custom_data] = tile
    created.append(tile)
    print_progress("created", kind, payload, board, monday_item, tile, progress=progress)
    return tile


def update_existing_tile(
    client: TimeBuzzerClient,
    existing: Dict[str, Any],
    payload: Dict[str, Any],
    execute: bool,
    updated: List[Dict[str, Any]],
    kind: str,
    board: Dict[str, Any],
    monday_item: Dict[str, Any],
    progress: bool,
) -> Dict[str, Any]:
    update_payload = merge_update_payload(existing, payload)
    if not execute:
        preview = dict(update_payload)
        preview["id"] = existing.get("id")
        updated.append(preview)
        # print_progress("would_update", kind, payload, board, monday_item, preview, progress=progress)
        return preview

    # print_progress("update_start", kind, payload, board, monday_item, existing, progress=progress)
    try:
        tile = client.update_tile(existing.get("id"), update_payload)
    except Exception as exc:
        print_progress("update_failed", kind, payload, board, monday_item, existing, error=str(exc), progress=progress)
        raise
    updated.append(tile)
    print_progress("updated", kind, payload, board, monday_item, tile, progress=progress)
    return tile


def archive_duplicate_tiles(
    client: TimeBuzzerClient,
    duplicates: List[Dict[str, Any]],
    execute: bool,
    archived_duplicates: List[Dict[str, Any]],
    kind: str,
    board: Dict[str, Any],
    monday_item: Dict[str, Any],
    progress: bool,
) -> None:
    for duplicate in duplicates:
        progress_payload = {
            "name": duplicate.get("name"),
            "description": duplicate.get("description"),
            "layer": duplicate.get("layer"),
            "parents": tile_parent_ids(duplicate) or None,
            "customData": duplicate.get("customData"),
        }
        archive_payload = merge_update_payload(duplicate, {**progress_payload, "archived": True})
        if not execute:
            preview = dict(archive_payload)
            archived_duplicates.append(preview)
            # print_progress("would_archive_duplicate", kind, progress_payload, board, monday_item, duplicate, progress=progress)
            continue

        # print_progress("archive_duplicate_start", kind, progress_payload, board, monday_item, duplicate, progress=progress)
        try:
            archived = client.update_tile(duplicate.get("id"), archive_payload)
        except Exception as exc:
            # print_progress(
            #     "archive_duplicate_failed",
            #     kind,
            #     progress_payload,
            #     board,
            #     monday_item,
            #     duplicate,
            #     error=str(exc),
            #     progress=progress,
            # )
            raise
        archived_duplicates.append(archived)
        # print_progress("archived_duplicate", kind, progress_payload, board, monday_item, archived, progress=progress)


def managed_layer_ids(layer_ids: LayerIds) -> set[int]:
    return {layer_ids.epic, layer_ids.item, layer_ids.subitem}


def layer_delete_rank(layer_ids: LayerIds, layer: Any) -> int:
    layer_int = as_int(layer)
    if layer_int == layer_ids.subitem:
        return 0
    if layer_int == layer_ids.item:
        return 1
    if layer_int == layer_ids.epic:
        return 2
    return 3


def stale_monday_tiles(
    existing_tiles: List[Dict[str, Any]],
    layer_ids: LayerIds,
    synced_tile_ids: set[str],
) -> List[Dict[str, Any]]:
    stale = []
    layer_id_set = managed_layer_ids(layer_ids)
    for tile in existing_tiles:
        tile_id_value = tile.get("id")
        if tile_id_value is None:
            continue
        if str(tile_id_value) in synced_tile_ids:
            continue
        layer = as_int(tile.get("layer"))
        if layer not in layer_id_set:
            continue
        if tile.get("archived") is True:
            continue
        stale.append(tile)

    return sorted(
        stale,
        key=lambda tile: (layer_delete_rank(layer_ids, tile.get("layer")), as_int(tile.get("id")) or 999999999),
    )


def delete_stale_tiles(
    client: TimeBuzzerClient,
    stale_tiles: List[Dict[str, Any]],
    execute: bool,
    deleted_stale: List[Dict[str, Any]],
    failed_stale: List[Dict[str, Any]],
    progress: bool,
) -> None:
    board = {"id": None, "name": "timeBuzzer"}
    monday_item = {"id": "stale", "name": "Removed from Monday sync"}

    for tile in stale_tiles:
        payload = {
            "name": tile.get("name"),
            "description": tile.get("description"),
            "layer": tile.get("layer"),
            "parents": tile_parent_ids(tile) or None,
            "customData": tile.get("customData"),
        }

        if not execute:
            deleted_stale.append(tile)
            # print_progress("would_delete_stale", "stale", payload, board, monday_item, tile, progress=progress)
            continue

        try:
            client.delete_tile(tile.get("id"))
        except Exception as exc:
            failed = {"tile": tile, "error": str(exc)}
            failed_stale.append(failed)
            print_progress("delete_stale_failed", "stale", payload, board, monday_item, tile, error=str(exc), progress=progress)
            continue

        deleted_stale.append(tile)
        print_progress("deleted_stale", "stale", payload, board, monday_item, tile, progress=progress)



def compact_monday_data(item: Dict[str, Any]) -> Dict[str, Any]:
    data = {
        "id": item.get("id"),
        "name": item.get("name"),
        "state": item.get("state"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "url": item.get("url"),
        "group": item.get("group"),
    }
    columns = []
    for column_value in item.get("column_values", []) or []:
        columns.append(
            {
                "id": column_value.get("id"),
                "type": column_value.get("type"),
                "text": column_display_value(column_value),
                "linked_item_ids": column_value.get("linked_item_ids"),
            }
        )
    if columns:
        data["columns"] = columns
    return {key: value for key, value in data.items() if value not in (None, "", [], {})}


def print_progress(
    action: str,
    kind: str,
    payload: Dict[str, Any],
    board: Dict[str, Any],
    monday_item: Dict[str, Any],
    tile: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    progress: bool = True,
) -> None:
    if not progress:
        return

    event = {
        "action": action,
        "kind": kind,
        "timebuzzer_id": (tile or {}).get("id"),
        "name": payload.get("name"),
        # "description": payload.get("description"),
        "layer": payload.get("layer"),
        "parents": payload.get("parents"),
        "board_id": board.get("id"),
        "board_name": board.get("name"),
        # "customData": payload.get("customData"),
        # "monday_data": compact_monday_data(monday_item),
    }
    if error:
        event["error"] = error
    print("PROGRESS " + json.dumps(event, ensure_ascii=True, default=str))


def monday_description(kind: str, item: Dict[str, Any], board: Dict[str, Any], extra: str = "") -> str:
    lines = [f"URL: {item.get('url')}" if item.get("url") else ""]
    if kind == "item" and extra:
        lines.append(extra)
    return compact_text(*lines)


def ensure_unassigned_epic_tile(
    timebuzzer: TimeBuzzerClient,
    existing_by_custom_data: Dict[str, Dict[str, Any]],
    existing_by_name_layer: Dict[Tuple[int, str], List[Dict[str, Any]]],
    execute: bool,
    created: List[Dict[str, Any]],
    updated: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
    archived_duplicates: List[Dict[str, Any]],
    layer_id: int,
    board: Dict[str, Any],
    progress: bool,
    cached_tile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if cached_tile:
        return cached_tile

    monday_item = {
        "id": "unassigned",
        "name": "No Epic",
        "state": "active",
    }
    payload = make_tile_payload(
        name="No Epic",
        layer_id=layer_id,
        custom_data="monday:epic:unassigned",
        color="#64748BFF",
        description=compact_text(
            "Source: Monday.com generated fallback",
            "Used for Sprint Backlog items that do not have an Epic relation.",
        ),
    )
    return ensure_tile(
        timebuzzer,
        payload,
        existing_by_custom_data,
        existing_by_name_layer,
        execute,
        created,
        updated,
        skipped,
        archived_duplicates,
        kind="epic",
        board=board,
        monday_item=monday_item,
        progress=progress,
    )


def sync_monday_to_timebuzzer(
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    latest_sprint_count: int = 2,
    execute: bool = True,
    layer_ids: Optional[LayerIds] = None,
    layer_ids_csv: Optional[str] = None,
    monday_token: Optional[str] = None,
    timebuzzer_api_key: Optional[str] = None,
    timebuzzer_base_url: str = TIMEBUZZER_BASE_URL,
    progress: bool = True,
) -> Dict[str, Any]:
    """
    Fetch Monday epics plus Sprint Backlog items/subitems from the latest sprints
    and create missing timeBuzzer tiles.

    Returns a summary dictionary. Set execute=True to call POST /tiles.
    """
    load_env_file()
    monday_token = monday_token or Config.MONDAY_API_TOKEN
    timebuzzer_api_key = timebuzzer_api_key or Config.TIMEBUZZER_API_KEY
    if not monday_token:
        raise RuntimeError("MONDAY_API_TOKEN is missing.")
    if not timebuzzer_api_key:
        raise RuntimeError("TIMEBUZZER_API_KEY is missing.")

    monday = MondayAPI(monday_token)
    timebuzzer = TimeBuzzerClient(timebuzzer_api_key, base_url=timebuzzer_base_url)

    existing_tiles = timebuzzer.get_all_tiles()
    layer_ids = layer_ids or parse_layer_ids(layer_ids_csv, existing_tiles=existing_tiles)
    existing_by_custom_data = existing_tiles_by_custom_data(existing_tiles)
    existing_by_name_layer = existing_tiles_by_name_layer(existing_tiles)
    if progress:
        logger.info(
            "PROGRESS "
            + json.dumps(
                {
                    "action": "layers_selected",
                    "layers": {"epic": layer_ids.epic, "item": layer_ids.item, "subitem": layer_ids.subitem},
                    "existing_tiles": len(existing_tiles),
                },
                ensure_ascii=True,
            )
        )

    epic_board_ref = monday.find_board(workspace_id, EPIC_BOARD_NAMES)
    backlog_board_ref = monday.find_board(workspace_id, SPRINT_BACKLOG_BOARD_NAMES)
    epic_board, epic_items = monday.get_board_items(str(epic_board_ref["id"]))
    backlog_board, backlog_items = monday.get_board_items(str(backlog_board_ref["id"]))
    backlog_meta = column_meta_by_id(backlog_board)

    sprint_board = None
    sprint_items: List[Dict[str, Any]] = []
    try:
        sprint_board_ref = monday.find_board(workspace_id, SPRINTS_BOARD_NAMES)
        sprint_board, sprint_items = monday.get_board_items(str(sprint_board_ref["id"]))
    except RuntimeError:
        logger.info("Sprints board was not found; inferring latest sprints from Sprint Backlog items.")

    sprint_keys = latest_sprint_keys(
        sprint_board=sprint_board,
        sprint_items=sprint_items,
        backlog_items=backlog_items,
        backlog_meta=backlog_meta,
        count=latest_sprint_count,
    )
    selected_items = filter_items_by_sprints(backlog_items, backlog_meta, sprint_keys)
    # if progress:
    #     print(
    #         "PROGRESS "
    #         + json.dumps(
    #             {
    #                 "action": "monday_data_loaded",
    #                 "workspace_id": workspace_id,
    #                 "boards": {
    #                     "epic": {"id": epic_board.get("id"), "name": epic_board.get("name")},
    #                     "sprint_backlog": {"id": backlog_board.get("id"), "name": backlog_board.get("name")},
    #                     "sprints": {"id": sprint_board.get("id"), "name": sprint_board.get("name")} if sprint_board else None,
    #                 },
    #                 "latest_sprint_keys": sprint_keys,
    #                 "counts": {
    #                     "epics": len(epic_items),
    #                     "backlog_items": len(backlog_items),
    #                     "selected_items": len(selected_items),
    #                     "selected_subitems": sum(len(item.get("subitems") or []) for item in selected_items),
    #                 },
    #             },
    #             ensure_ascii=True,
    #         )
    #     )

    created: List[Dict[str, Any]] = []
    updated: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    archived_duplicates: List[Dict[str, Any]] = []
    deleted_stale: List[Dict[str, Any]] = []
    failed_stale_deletes: List[Dict[str, Any]] = []
    synced_tile_ids: set[str] = set()

    epic_tiles_by_id: Dict[str, Dict[str, Any]] = {}
    epic_tiles_by_name: Dict[str, Dict[str, Any]] = {}
    for epic in epic_items:
        custom_data = f"monday:epic:{epic.get('id')}"
        payload = make_tile_payload(
            name=epic.get("name") or "Untitled Epic",
            layer_id=layer_ids.epic,
            custom_data=custom_data,
            color="#4F46E5FF",
            description=monday_description("epic", epic, epic_board),
        )
        tile = ensure_tile(
            timebuzzer,
            payload,
            existing_by_custom_data,
            existing_by_name_layer,
            execute,
            created,
            updated,
            skipped,
            archived_duplicates,
            kind="epic",
            board=epic_board,
            monday_item=epic,
            progress=progress,
        )
        if tile.get("id") is not None:
            synced_tile_ids.add(str(tile.get("id")))
        epic_tiles_by_id[str(epic.get("id"))] = tile
        epic_tiles_by_name[normalize_name(epic.get("name"))] = tile

    fallback_epic_tile: Optional[Dict[str, Any]] = None

    item_tiles_by_id: Dict[str, Dict[str, Any]] = {}
    for item in selected_items:
        parent_ids = find_epic_parent_ids(item, backlog_meta, epic_tiles_by_id, epic_tiles_by_name)
        if not parent_ids:
            fallback_epic_tile = ensure_unassigned_epic_tile(
                timebuzzer=timebuzzer,
                existing_by_custom_data=existing_by_custom_data,
                existing_by_name_layer=existing_by_name_layer,
                execute=execute,
                created=created,
                updated=updated,
                skipped=skipped,
                archived_duplicates=archived_duplicates,
                layer_id=layer_ids.epic,
                board=epic_board,
                progress=progress,
                cached_tile=fallback_epic_tile,
            )
            fallback_parent_id = tile_id(fallback_epic_tile or {})
            if fallback_epic_tile and fallback_epic_tile.get("id") is not None:
                synced_tile_ids.add(str(fallback_epic_tile.get("id")))
            parent_ids = [fallback_parent_id] if fallback_parent_id else None

        sprint_names = ", ".join(sorted({ref.name for ref in item_sprint_refs(item, backlog_meta) if ref.name}))
        payload = make_tile_payload(
            name=item.get("name") or "Untitled Item",
            layer_id=layer_ids.item,
            custom_data=f"monday:item:{item.get('id')}",
            parents=parent_ids,
            color="#0891B2FF",
            description=monday_description("item", item, backlog_board, f"Sprint: {sprint_names}" if sprint_names else ""),
        )
        tile = ensure_tile(
            timebuzzer,
            payload,
            existing_by_custom_data,
            existing_by_name_layer,
            execute,
            created,
            updated,
            skipped,
            archived_duplicates,
            kind="item",
            board=backlog_board,
            monday_item=item,
            progress=progress,
        )
        if tile.get("id") is not None:
            synced_tile_ids.add(str(tile.get("id")))
        item_tiles_by_id[str(item.get("id"))] = tile

    for item in selected_items:
        parent_tile = item_tiles_by_id.get(str(item.get("id")))
        parent_id = tile_id(parent_tile or {})
        for subitem in item.get("subitems") or []:
            payload = make_tile_payload(
                name=subitem.get("name") or "Untitled Subitem",
                layer_id=layer_ids.subitem,
                custom_data=f"monday:subitem:{subitem.get('id')}",
                parents=[parent_id] if parent_id else None,
                color="#16A34AFF",
                description=monday_description("subitem", subitem, backlog_board, f"Parent item: {item.get('name')}"),
            )
            subitem_tile = ensure_tile(
                timebuzzer,
                payload,
                existing_by_custom_data,
                existing_by_name_layer,
                execute,
                created,
                updated,
                skipped,
                archived_duplicates,
                kind="subitem",
                board=backlog_board,
                monday_item=subitem,
                progress=progress,
            )
            if subitem_tile.get("id") is not None:
                synced_tile_ids.add(str(subitem_tile.get("id")))

    stale_tiles = stale_monday_tiles(existing_tiles, layer_ids, synced_tile_ids)
    delete_stale_tiles(timebuzzer, stale_tiles, execute, deleted_stale, failed_stale_deletes, progress)

    return {
        "mode": "execute" if execute else "dry_run",
        "workspace_id": workspace_id,
        "layers": {"epic": layer_ids.epic, "item": layer_ids.item, "subitem": layer_ids.subitem},
        "sprint_filter": "latest_sprints",
        "stale_tile_cleanup": "id_based",
        "boards": {
            "epic": {"id": epic_board.get("id"), "name": epic_board.get("name")},
            "sprint_backlog": {"id": backlog_board.get("id"), "name": backlog_board.get("name")},
            "sprints": {"id": sprint_board.get("id"), "name": sprint_board.get("name")} if sprint_board else None,
        },
        "latest_sprint_keys": sprint_keys,
        "monday_counts": {
            "epics": len(epic_items),
            "backlog_items": len(backlog_items),
            "selected_items": len(selected_items),
            "selected_subitems": sum(len(item.get("subitems") or []) for item in selected_items),
        },
        "timebuzzer_counts": {
            "created_or_would_create": len(created),
            "updated_or_would_update": len(updated),
            "archived_duplicates_or_would_archive": len(archived_duplicates),
            "deleted_stale_or_would_delete": len(deleted_stale),
            "failed_stale_deletes": len(failed_stale_deletes),
            "skipped_existing": len(skipped),
        },
        "created_or_would_create": created,
        "updated_or_would_update": updated,
        "archived_duplicates_or_would_archive": archived_duplicates,
        "deleted_stale_or_would_delete": deleted_stale,
        "failed_stale_deletes": failed_stale_deletes,
        "skipped_existing": skipped,
    }


def find_epic_parent_ids(
    item: Dict[str, Any],
    backlog_meta: Dict[str, Dict[str, Any]],
    epic_tiles_by_id: Dict[str, Dict[str, Any]],
    epic_tiles_by_name: Dict[str, Dict[str, Any]],
) -> Optional[List[int]]:
    parent_ids: List[int] = []
    for ref in item_epic_refs(item, backlog_meta):
        tile = None
        if ref.key.startswith("id:"):
            tile = epic_tiles_by_id.get(ref.key.split(":", 1)[1])
        if not tile and ref.name:
            tile = epic_tiles_by_name.get(normalize_name(ref.name))

        parent_id = tile_id(tile or {})
        if parent_id and parent_id not in parent_ids:
            parent_ids.append(parent_id)

    return parent_ids or None


def parse_args() -> argparse.Namespace:
    load_env_file()
    parser = argparse.ArgumentParser(description="Sync Monday.com sprint backlog data into timeBuzzer tiles.")
    parser.add_argument("--workspace-id", default=Config.WORKSPACE_ID_2 or DEFAULT_WORKSPACE_ID)
    parser.add_argument("--latest-sprints", type=int, default=2)
    parser.add_argument("--layer-ids", help="Comma-separated timeBuzzer layer IDs: epic,item,subitem.")
    parser.add_argument("--base-url", default=Config.TIMEBUZZER_BASE_URL or TIMEBUZZER_BASE_URL)
    parser.add_argument("--execute", action="store_true", help="Create missing timeBuzzer tiles. Defaults to dry run.")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")

    summary = sync_monday_to_timebuzzer(
        workspace_id=str(args.workspace_id),
        latest_sprint_count=args.latest_sprints,
        execute=args.execute,
        layer_ids_csv=args.layer_ids,
        timebuzzer_base_url=args.base_url,
    )

    created = summary["timebuzzer_counts"]["created_or_would_create"]
    updated = summary["timebuzzer_counts"]["updated_or_would_update"]
    archived = summary["timebuzzer_counts"]["archived_duplicates_or_would_archive"]
    stale_deleted = summary["timebuzzer_counts"]["deleted_stale_or_would_delete"]
    stale_failed = summary["timebuzzer_counts"]["failed_stale_deletes"]
    skipped = summary["timebuzzer_counts"]["skipped_existing"]
    mode = summary["mode"]
    # print(f"timeBuzzer sync complete ({mode}).")
    # print(f"Workspace: {summary['workspace_id']}")
    # print(f"Layers: {summary['layers']}")
    # print(f"Monday counts: {summary['monday_counts']}")
    # print(
    #     "timeBuzzer: "
    #     f"{created} {'created' if args.execute else 'would be created'}, "
    #     f"{updated} {'updated' if args.execute else 'would be updated'}, "
    #     f"{archived} duplicate(s) {'archived' if args.execute else 'would be archived'}, "
    #     f"{stale_deleted} stale tile(s) {'deleted' if args.execute else 'would be deleted'}, "
    #     f"{stale_failed} stale delete failure(s), "
    #     f"{skipped} skipped existing"
    # )
    if not args.execute:
        print("Dry run only. Re-run with --execute to create missing tiles.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
