"""
Delete all three TimeBuzzer tile layers from timeBuzzer.

Default behavior is a dry run. The script lists the tiles it would delete and
only performs DELETE requests when both --execute and --yes are supplied.

Examples:
  python del.py
  python del.py --execute --yes
  python del.py --monday-only --execute --yes
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, Iterable, List, Optional

import requests


DEFAULT_BASE_URL = "https://my.timebuzzer.com/open-api"


def load_env_file(path: str = ".env") -> None:
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


def normalize_authorization_header(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    value = value.strip()
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if value.lower().startswith(("apikey ", "bearer ")):
        return value
    return f"Bearer {value}" if value else None


class TimeBuzzerDeleteClient:
    def __init__(self, api_key: Optional[str] = None, base_url: str = DEFAULT_BASE_URL):
        load_env_file()
        authorization = normalize_authorization_header(api_key or os.environ.get("TIMEBUZZER_API_KEY"))
        if not authorization:
            raise RuntimeError("TIMEBUZZER_API_KEY is missing. Add it to .env or the environment.")

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
            timeout=30,
            **kwargs,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} failed: {response.status_code} {response.text[:1000]}")
        if not response.content:
            return None
        return response.json()

    def get_tiles(self, archived: Optional[bool] = False) -> List[Dict[str, Any]]:
        params = {}
        if archived is not None:
            params["archived"] = "true" if archived else "false"

        data = self.request("GET", "/tiles", params=params)
        if isinstance(data, list):
            return [tile for tile in data if isinstance(tile, dict)]
        if isinstance(data, dict):
            for key in ("tiles", "data", "items", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return [tile for tile in value if isinstance(tile, dict)]
            return [data]
        return []

    def get_all_tiles(self, include_archived: bool = True) -> List[Dict[str, Any]]:
        if not include_archived:
            return self.get_tiles(archived=False)

        by_id: Dict[str, Dict[str, Any]] = {}
        for archived in (False, True):
            for tile in self.get_tiles(archived=archived):
                tile_id = str(tile.get("id"))
                by_id[tile_id] = tile
        return list(by_id.values())

    def delete_tile(self, tile_id: Any) -> Any:
        return self.request("DELETE", f"/tiles/{tile_id}")


def parse_layer_ids(value: Optional[str]) -> Optional[List[int]]:
    if not value:
        return None
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def infer_layer_ids(tiles: List[Dict[str, Any]], explicit_layer_ids: Optional[str]) -> List[int]:
    parsed = parse_layer_ids(explicit_layer_ids)
    if parsed:
        return parsed

    env_layer_ids = parse_layer_ids(os.environ.get("TIMEBUZZER_LAYER_IDS"))
    if env_layer_ids:
        return env_layer_ids

    env_named_layers = [
        os.environ.get("TIMEBUZZER_EPIC_LAYER_ID"),
        os.environ.get("TIMEBUZZER_ITEM_LAYER_ID"),
        os.environ.get("TIMEBUZZER_SUBITEM_LAYER_ID"),
    ]
    if all(env_named_layers):
        return [int(layer_id) for layer_id in env_named_layers if layer_id]

    layers = sorted({int(tile["layer"]) for tile in tiles if str(tile.get("layer", "")).isdigit()})
    if len(layers) < 3:
        raise RuntimeError(
            "Could not infer three layer IDs. Set TIMEBUZZER_LAYER_IDS like 193331,193332,193333 "
            "or pass --layer-ids."
        )
    return layers[:3]


def select_tiles(
    tiles: List[Dict[str, Any]],
    layer_ids: Iterable[int],
    monday_only: bool,
) -> List[Dict[str, Any]]:
    layer_id_set = {int(layer_id) for layer_id in layer_ids}
    selected = []
    for tile in tiles:
        layer = int(tile.get("layer") or -1)
        if layer not in layer_id_set:
            continue
        if monday_only and not str(tile.get("customData") or "").startswith("monday:"):
            continue
        selected.append(tile)
    return sorted(selected, key=lambda item: int(item.get("layer") or -1), reverse=True)


def print_tile_list(tiles: List[Dict[str, Any]], layer_ids: List[int]) -> None:
    print(f"Layer ids selected: {', '.join(str(layer_id) for layer_id in layer_ids)}")
    print(f"Tiles selected: {len(tiles)}")

    counts: Dict[int, int] = {}
    for tile in tiles:
        layer = int(tile.get("layer") or -1)
        counts[layer] = counts.get(layer, 0) + 1

    for layer_id in sorted(counts, reverse=True):
        print(f"- layer {layer_id}: {counts[layer_id]} tile(s)")

    print("\nDelete order preview: deepest layer first.")
    for tile in tiles:
        print(
            f"- layer={tile.get('layer')} "
            f"id={tile.get('id')} "
            f"name={tile.get('name')!r} "
            f"customData={tile.get('customData')!r} "
            f"archived={tile.get('archived')}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete all three tile layers from timeBuzzer.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--layer-ids",
        help="Comma-separated layer IDs to delete. Defaults to TIMEBUZZER_LAYER_IDS, named layer env vars, or the first three layer IDs found.",
    )
    parser.add_argument("--active-only", action="store_true", help="Only target active/non-archived tiles.")
    parser.add_argument("--monday-only", action="store_true", help="Only target tiles whose customData starts with monday:.")
    parser.add_argument("--execute", action="store_true", help="Actually call DELETE /tiles/{id}. Without this, only prints a dry run.")
    parser.add_argument("--yes", action="store_true", help="Required together with --execute to confirm deletion.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = TimeBuzzerDeleteClient(base_url=args.base_url)
    tiles = client.get_all_tiles(include_archived=not args.active_only)
    layer_ids = infer_layer_ids(tiles, args.layer_ids)
    selected_tiles = select_tiles(tiles, layer_ids, args.monday_only)

    print_tile_list(selected_tiles, layer_ids)

    if not args.execute:
        print("\nDry run only. Re-run with --execute --yes to delete these tiles.")
        return 0
    if not args.yes:
        print("\nRefusing to delete. Add --yes together with --execute.")
        return 2

    deleted = []
    failed = []
    for tile in selected_tiles:
        tile_id = tile.get("id")
        try:
            client.delete_tile(tile_id)
            deleted.append(tile)
            print(f"deleted layer={tile.get('layer')} id={tile_id} name={tile.get('name')!r}")
        except Exception as exc:
            failed.append({"tile": tile, "error": str(exc)})
            print(f"failed layer={tile.get('layer')} id={tile_id} name={tile.get('name')!r}: {exc}")

    print(f"\nDeleted: {len(deleted)}")
    print(f"Failed: {len(failed)}")
    if failed:
        print("Some tiles may have linked time entries, child relations, or API restrictions and could not be deleted.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
