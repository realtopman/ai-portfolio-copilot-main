import json
import logging
import os
import threading
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request

from api import context
from services import timebuzzer as timebuzzer_sync
from utils.hours import find_hours_column_id
from utils.update_cache import mark_update_made as _mark_update_made


logger = logging.getLogger(__name__)
bp = Blueprint("timebuzzer", __name__)

# ==================== SYNC TIMEBUZZER AND MONDAY ENDPOINT ====================
def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def parse_int(value, default):
    if value in (None, ''):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def request_value(payload, key, default=None):
    if key in payload and payload.get(key) is not None:
        return payload.get(key)
    return request.args.get(key, default)


def validate_layer_ids_csv(layer_ids_csv):
    if not layer_ids_csv:
        return None

    parts = [part.strip() for part in str(layer_ids_csv).split(",") if part.strip()]
    if len(parts) != 3:
        return "layer_ids must contain exactly three comma-separated IDs: epic,item,subitem"

    try:
        [int(part) for part in parts]
    except ValueError:
        return "layer_ids must contain numeric IDs only: epic,item,subitem"

    return None


def start_sync_timebuzzer_tiles(workspace_id,
        latest_sprint_count,
        execute,
        layer_ids_csv,
        timebuzzer_base_url,
        progress,
        ):
    try:
        summary = timebuzzer_sync.sync_monday_to_timebuzzer(
            workspace_id=workspace_id,
            latest_sprint_count=latest_sprint_count,
            execute=execute,
            layer_ids_csv=layer_ids_csv,
            timebuzzer_base_url=timebuzzer_base_url,
            progress=progress,
        )

        counts = summary["timebuzzer_counts"]
        created = counts["created_or_would_create"]
        updated = counts["updated_or_would_update"]
        archived = counts["archived_duplicates_or_would_archive"]
        stale_deleted = counts["deleted_stale_or_would_delete"]
        stale_failed = counts["failed_stale_deletes"]
        skipped = counts["skipped_existing"]
        mode = summary["mode"]
        logger.info("timeBuzzer sync complete (%s).", mode)
        logger.info("Workspace: %s", summary["workspace_id"])
        logger.info("Layers: %s", summary["layers"])
        logger.info("Monday counts: %s", summary["monday_counts"])
        logger.info(
            "timeBuzzer: %s %s, %s %s, %s duplicate(s) %s, "
            "%s stale tile(s) %s, %s stale delete failure(s), %s skipped existing",
            created,
            "created" if execute else "would be created",
            updated,
            "updated" if execute else "would be updated",
            archived,
            "archived" if execute else "would be archived",
            stale_deleted,
            "deleted" if execute else "would be deleted",
            stale_failed,
            skipped,
        )
    except Exception as exc:
        logger.error("TimeBuzzer background sync failed: %s", exc, exc_info=True)



TIMEBUZZER_ACTIVITY_CACHE = {}
TIMEBUZZER_ACTIVITY_CACHE_LIMIT = 1000
DEFAULT_TIMEBUZZER_ACTIVITY_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    ".timebuzzer_activity_cache.json",
)
TIMEBUZZER_ACTIVITY_CACHE_LOCK = threading.Lock()


class TimeBuzzerActivityDetailsUnavailable(Exception):
    pass


def timebuzzer_activity_cache_path():
    return current_app.config.get("TIMEBUZZER_ACTIVITY_CACHE_FILE") or DEFAULT_TIMEBUZZER_ACTIVITY_CACHE_PATH


def load_timebuzzer_activity_cache():
    if TIMEBUZZER_ACTIVITY_CACHE:
        return
    try:
        with open(timebuzzer_activity_cache_path(), "r", encoding="utf-8") as cache_file:
            data = json.load(cache_file)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        logger.warning("Could not load TimeBuzzer activity cache: %s", exc)
        return
    if isinstance(data, dict):
        TIMEBUZZER_ACTIVITY_CACHE.update({str(key): value for key, value in data.items() if isinstance(value, dict)})


def save_timebuzzer_activity_cache():
    try:
        with open(timebuzzer_activity_cache_path(), "w", encoding="utf-8") as cache_file:
            json.dump(TIMEBUZZER_ACTIVITY_CACHE, cache_file)
    except OSError as exc:
        logger.warning("Could not save TimeBuzzer activity cache: %s", exc)


def remember_timebuzzer_activity(payload):
    activity_id = timebuzzer_activity_payload_id(payload) if isinstance(payload, dict) else None
    if activity_id is None:
        return
    with TIMEBUZZER_ACTIVITY_CACHE_LOCK:
        load_timebuzzer_activity_cache()
        TIMEBUZZER_ACTIVITY_CACHE[str(activity_id)] = dict(payload)
        while len(TIMEBUZZER_ACTIVITY_CACHE) > TIMEBUZZER_ACTIVITY_CACHE_LIMIT:
            TIMEBUZZER_ACTIVITY_CACHE.pop(next(iter(TIMEBUZZER_ACTIVITY_CACHE)))
        save_timebuzzer_activity_cache()


def cached_timebuzzer_activity(activity_id):
    with TIMEBUZZER_ACTIVITY_CACHE_LOCK:
        load_timebuzzer_activity_cache()
        cached = TIMEBUZZER_ACTIVITY_CACHE.get(str(activity_id))
    return dict(cached) if cached else None


def parse_timebuzzer_datetime(value):
    if not value:
        raise ValueError("TimeBuzzer activity date is missing")
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def timebuzzer_activity_duration_hours(payload):
    start = parse_timebuzzer_datetime(payload.get("startDate"))
    end = parse_timebuzzer_datetime(payload.get("endDate"))
    seconds = (end - start).total_seconds()
    if seconds < 0:
        raise ValueError("TimeBuzzer activity endDate is before startDate")
    return round(seconds / 3600, 4)


def monday_refs_from_timebuzzer_tiles(tiles):
    refs = {}
    for tile in tiles or []:
        custom_data = str(tile.get("customData") or "")
        parts = custom_data.split(":", 2)
        if len(parts) != 3 or parts[0] != "monday" or parts[1] not in {"epic", "item", "subitem"}:
            continue
        refs[parts[1]] = {
            "id": parts[2],
            "tile_id": tile.get("id"),
            "tile_name": tile.get("name"),
            "customData": custom_data,
        }
    return refs


def timebuzzer_tile_ids_from_payload(payload):
    ids = []
    for tile in payload.get("tiles") or []:
        tile_id = tile.get("id")
        if tile_id is not None:
            ids.append(int(tile_id))
    return ids


def timebuzzer_tile_filter_from_payload(payload):
    tile_filter = {}
    for tile in payload.get("tiles") or []:
        tile_id = tile.get("id")
        layer_index = tile.get("layerIndex")
        if tile_id is None or layer_index is None:
            continue
        tile_filter.setdefault(str(layer_index), []).append(int(tile_id))
    return tile_filter


def activity_value(activity, *names):
    for name in names:
        if name in activity:
            return activity.get(name)
    return None


def activity_tile_ids(activity):
    tiles = activity.get("tiles") or activity.get("tiles_") or []
    ids = []
    for tile in tiles:
        if isinstance(tile, dict):
            tile_id = tile.get("id")
        else:
            tile_id = tile
        if tile_id is not None:
            ids.append(int(tile_id))
    return ids


def is_same_timebuzzer_activity(payload, activity):
    activity_user_id = activity_value(activity, "userId", "user_id")
    if activity_user_id is None and isinstance(activity.get("user"), dict):
        activity_user_id = activity["user"].get("id")
    if str(activity_user_id) != str(payload.get("userId")):
        return False

    return sorted(activity_tile_ids(activity)) == sorted(timebuzzer_tile_ids_from_payload(payload))


def timebuzzer_activity_payload_id(payload):
    return activity_value(payload, "id", "activityId", "activity_id")


def timebuzzer_api_client():
    api_key = current_app.config.get("TIMEBUZZER_API_KEY")
    if not api_key:
        raise RuntimeError("TIMEBUZZER_API_KEY is missing.")
    return timebuzzer_sync.TimeBuzzerClient(
        api_key,
        base_url=current_app.config.get("TIMEBUZZER_BASE_URL") or timebuzzer_sync.TIMEBUZZER_BASE_URL,
    )


def normalize_timebuzzer_activity_payload(payload, activity_type):
    normalized = dict(payload or {})
    activity_id = timebuzzer_activity_payload_id(normalized)
    if activity_id is not None:
        normalized.setdefault("id", activity_id)

    has_activity_details = (
        normalized.get("startDate")
        and normalized.get("endDate")
        and normalized.get("tiles")
        and normalized.get("userId") is not None
    )
    if has_activity_details:
        remember_timebuzzer_activity(normalized)
        return normalized

    if activity_type != "delete":
        return normalized
    if activity_id is None:
        raise ValueError("TimeBuzzer delete payload is missing activityId")

    cached = cached_timebuzzer_activity(activity_id)
    if cached:
        cached.setdefault("id", activity_id)
        cached.setdefault("activityId", activity_id)
        logger.info("Using cached TimeBuzzer deleted activity details for activityId=%s", activity_id)
        return cached

    try:
        fetched = timebuzzer_api_client().get_activity(activity_id)
    except Exception as exc:
        message = str(exc)
        if "404" in message or "not found" in message.lower():
            raise TimeBuzzerActivityDetailsUnavailable(
                f"TimeBuzzer deleted activity {activity_id} was not found and is not in the local cache. "
                "Cannot determine user, tiles, duration, or Monday target for this delete webhook."
            ) from exc
        raise
    fetched_payload = dict(fetched)
    fetched_payload.setdefault("id", activity_id)
    fetched_payload.setdefault("activityId", activity_id)
    remember_timebuzzer_activity(fetched_payload)
    logger.info(
        "Fetched TimeBuzzer deleted activity details for activityId=%s: userId=%s, tiles=%s, startDate=%s, endDate=%s",
        activity_id,
        fetched_payload.get("userId"),
        [tile.get("id") for tile in fetched_payload.get("tiles") or [] if isinstance(tile, dict)],
        fetched_payload.get("startDate"),
        fetched_payload.get("endDate"),
    )
    return fetched_payload


def matching_timebuzzer_activities(payload):
    filters = {
        "userIds": [int(payload.get("userId"))],
        "tiles": timebuzzer_tile_filter_from_payload(payload),
    }
    activities = timebuzzer_api_client().filter_activities(filters, count=100)
    return [activity for activity in activities if is_same_timebuzzer_activity(payload, activity)]


def timebuzzer_activity_duration_from_activity(activity):
    start = activity_value(activity, "startDate", "start_date", "start")
    end = activity_value(activity, "endDate", "end_date", "end")
    return timebuzzer_activity_duration_hours({
        "startDate": start,
        "endDate": end,
    })


def timebuzzer_activity_id(activity):
    return activity_value(activity, "id", "activityId", "activity_id")


def timebuzzer_activity_duration_summary(payload, activity_type):
    current_activity_id = timebuzzer_activity_payload_id(payload)
    current_duration_hours = timebuzzer_activity_duration_hours(payload)
    matching_activities = matching_timebuzzer_activities(payload)

    matched_duration_hours = 0.0
    included_current_activity = False
    counted_activity_ids = []
    excluded_activity_ids = []
    for activity in matching_activities:
        activity_id = timebuzzer_activity_id(activity)
        if (
            activity_type == "delete"
            and current_activity_id is not None
            and str(activity_id) == str(current_activity_id)
        ):
            excluded_activity_ids.append(activity_id)
            continue
        matched_duration_hours += timebuzzer_activity_duration_from_activity(activity)
        counted_activity_ids.append(activity_id)
        if current_activity_id is not None and str(activity_id) == str(current_activity_id):
            included_current_activity = True

    total_duration_hours = matched_duration_hours
    if activity_type in {"new", "edit"} and not included_current_activity:
        total_duration_hours += current_duration_hours

    return {
        "current_duration_hours": round(current_duration_hours, 4),
        "matched_duration_hours": round(matched_duration_hours, 4),
        "remaining_duration_hours": round(matched_duration_hours, 4),
        "total_duration_hours": round(total_duration_hours, 4),
        "matching_activity_count": len(matching_activities),
        "counted_activity_count": len(counted_activity_ids),
        "counted_activity_ids": counted_activity_ids,
        "excluded_activity_ids": excluded_activity_ids,
        "included_current_activity": included_current_activity,
    }


def actual_hours_from_target(target, column_id):
    for column_value in target.get("column_values") or []:
        if str(column_value.get("id")) != str(column_id):
            continue
        value = column_value.get("value")
        text = column_value.get("text")
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
            if isinstance(parsed, dict):
                return float(parsed.get("value") or 0)
            if parsed not in (None, ""):
                return float(parsed)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        try:
            return float(str(text or "0").replace(",", "").replace(" Hrs", "").replace("Hrs", "").strip() or 0)
        except ValueError:
            return 0.0
    return 0.0


def update_monday_actual_hours_from_timebuzzer(payload, activity_type):
    if context.monday_api is None:
        raise RuntimeError("Monday API is not initialized.")

    try:
        payload = normalize_timebuzzer_activity_payload(payload, activity_type)
    except TimeBuzzerActivityDetailsUnavailable as exc:
        logger.warning("TimeBuzzer %s activity skipped: %s", activity_type, exc)
        return {
            "status": "skipped",
            "reason": str(exc),
            "activity_type": activity_type,
            "timebuzzer_entry_id": timebuzzer_activity_payload_id(payload),
        }, 200

    duration_hours = timebuzzer_activity_duration_hours(payload)
    refs = monday_refs_from_timebuzzer_tiles(payload.get("tiles") or [])

    if "subitem" in refs:
        target_type = "subitem"
        target_id = refs["subitem"]["id"]
        target = context.monday_api.get_subitem_with_parent(target_id)
        if not target:
            return {
                "status": "error",
                "error": f"Monday subitem {target_id} was not found.",
            }, 404

        parent_item = target.get("parent_item") or {}
        if refs.get("item") and str(parent_item.get("id")) != str(refs["item"]["id"]):
            item_ref_id = refs["item"]["id"]
            return {
                "status": "error",
                "error": (
                    f"Subitem {target_id} does not belong to item {item_ref_id} "
                    "from the TimeBuzzer tile path."
                ),
            }, 400
    elif "item" in refs:
        target_type = "item"
        target_id = refs["item"]["id"]
        target = context.monday_api.get_item_with_columns(target_id)
        if not target:
            return {
                "status": "error",
                "error": f"Monday item {target_id} was not found.",
            }, 404
    else:
        return {
            "status": "skipped",
            "reason": "No mapped Monday item/subitem tile was found in TimeBuzzer payload.",
            "timebuzzer_entry_id": timebuzzer_activity_payload_id(payload),
        }, 200

    board = target.get("board") or {}
    board_id = str(board.get("id") or "")
    if not board_id:
        return {
            "status": "error",
            "error": f"Could not determine Monday board for {target_type} {target_id}.",
        }, 400

    columns = context.monday_api.get_board_columns(board_id)
    actual_col_id, actual_col_title = find_hours_column_id(columns, "actual")
    if not actual_col_id:
        return {
            "status": "error",
            "error": f"Actual Hrs column was not found on Monday board {board_id}.",
            "target_type": target_type,
            "target_id": target_id,
        }, 404

    original_actual_hours = actual_hours_from_target(target, actual_col_id)
    duration_summary = timebuzzer_activity_duration_summary(payload, activity_type)
    matching_activity_exists = duration_summary["matching_activity_count"] > 0
    actual_hours_to_write = duration_summary["total_duration_hours"]
    logger.info(
        "____________________TimeBuzzer %s activity writing Monday Actual Hrs=%s for %s %s on board %s "
        "(entry_id=%s, current_duration=%s, remaining_duration=%s, matching_count=%s, "
        "counted_count=%s, counted_ids=%s, excluded_ids=%s, included_current=%s, "
        "original_actual=%s, column=%s/%s)",
        activity_type,
        actual_hours_to_write,
        target_type,
        target_id,
        board_id,
        timebuzzer_activity_payload_id(payload),
        duration_summary["current_duration_hours"],
        duration_summary["matched_duration_hours"],
        duration_summary["matching_activity_count"],
        duration_summary["counted_activity_count"],
        duration_summary["counted_activity_ids"],
        duration_summary["excluded_activity_ids"],
        duration_summary["included_current_activity"],
        original_actual_hours,
        actual_col_title,
        actual_col_id,
    )

    value = json.dumps({"value": actual_hours_to_write, "unit": None})
    updated = context.monday_api.update_column_value(board_id, target_id, actual_col_id, value, "numeric")
    if updated:
        _mark_update_made(target_id, actual_col_id)

    return {
        "status": "success" if updated else "error",
        "activity_type": activity_type,
        "timebuzzer_entry_id": timebuzzer_activity_payload_id(payload),
        "duration_hours": duration_hours,
        "matching_activity_exists": matching_activity_exists,
        "matching_activity_count": duration_summary["matching_activity_count"],
        "counted_activity_count": duration_summary["counted_activity_count"],
        "counted_activity_ids": duration_summary["counted_activity_ids"],
        "matched_duration_hours": duration_summary["matched_duration_hours"],
        "remaining_duration_hours": duration_summary["remaining_duration_hours"],
        "excluded_activity_ids": duration_summary["excluded_activity_ids"],
        "included_current_activity": duration_summary["included_current_activity"],
        "original_actual_hours": original_actual_hours,
        "actual_hours_written": actual_hours_to_write,
        "target_type": target_type,
        "target_id": target_id,
        "target_name": target.get("name"),
        "board_id": board_id,
        "actual_hours_column_id": actual_col_id,
        "actual_hours_column_title": actual_col_title,
        "monday_refs": refs,
    }, 200 if updated else 500


def handle_timebuzzer_activity(activity_type):
    payload = request.get_json(silent=True) or {}
    logger.info("TimeBuzzer %s activity payload: %s", activity_type, payload)

    try:
        result, status_code = update_monday_actual_hours_from_timebuzzer(payload, activity_type)
    except Exception as exc:
        logger.error("TimeBuzzer %s activity handling failed: %s", activity_type, exc, exc_info=True)
        return jsonify({"status": "error", "error": str(exc)}), 500

    if status_code >= 400:
        logger.warning("TimeBuzzer %s activity was not applied: %s", activity_type, result)
    else:
        logger.info("TimeBuzzer %s activity applied: %s", activity_type, result)
    return jsonify(result), status_code


@bp.route("/api/timebuzzer/newActivity", methods=["POST"])
def receive_timebuzzer_new_activity():
    return handle_timebuzzer_activity("new")


@bp.route("/api/timebuzzer/editActivity", methods=["POST"])
def receive_timebuzzer_eidt_activity():
    return handle_timebuzzer_activity("edit")


receive_timebuzzer_edit_activity = receive_timebuzzer_eidt_activity


@bp.route("/api/timebuzzer/deleteActivity", methods=["POST"])
def receive_timebuzzer_delete_activity():
    return handle_timebuzzer_activity("delete")


@bp.route("/api/timebuzzer/sync", methods=["GET", "POST"])
def sync_timebuzzer_tiles():
    """
    Sync Monday.com epics, Sprint Backlog items, and subitems into timeBuzzer.

    Body/query options:
      workspace_id: Monday workspace ID. Defaults to WORKSPACE_ID_2.
      latest_sprints: number of latest sprints to sync. Defaults to 2.
      execute: false for dry-run, true to create/update/delete tiles.
      layer_ids: comma-separated timeBuzzer layer IDs: epic,item,subitem.
      base_url: optional timeBuzzer API base URL.
      progress: print progress events to stdout. Defaults to false for Flask.
    """
    payload = request.get_json(silent=True) or {}

    workspace_id = str(
        request_value(payload, "workspace_id", current_app.config.get("WORKSPACE_ID_2") or timebuzzer_sync.DEFAULT_WORKSPACE_ID)
    )
    latest_sprints = parse_int(request_value(payload, "latest_sprints"), 2)
    execute = parse_bool(request_value(payload, "execute"), True)
    progress = parse_bool(request_value(payload, "progress"), True)
    layer_ids_arg = request_value(payload, "layer_ids")
    base_url = request_value(payload, "base_url", current_app.config.get("TIMEBUZZER_BASE_URL") or timebuzzer_sync.TIMEBUZZER_BASE_URL)

    if latest_sprints < 1:
        return jsonify({
            "status": "error",
            "error": "latest_sprints must be 1 or greater",
        }), 400

    configured_layer_ids = current_app.config.get("TIMEBUZZER_LAYER_IDS")
    layer_ids_error = validate_layer_ids_csv(layer_ids_arg or configured_layer_ids)
    if layer_ids_error:
        return jsonify({
            "status": "error",
            "error": layer_ids_error,
        }), 400

    try:
        thread = threading.Thread(
            target=start_sync_timebuzzer_tiles,
            args=(
                workspace_id,
                latest_sprints,
                execute,
                layer_ids_arg,
                base_url,
                progress,
            ),
        )
        thread.daemon = True
        thread.start()
        return jsonify({
            "status": "started",
            "message": "TimeBuzzer sync has started in the background.",
        }), 202
    except Exception as exc:
        logger.error("TimeBuzzer sync endpoint failed: %s", exc, exc_info=True)
        return jsonify({
            "status": "error",
            "error": str(exc),
        }), 500
