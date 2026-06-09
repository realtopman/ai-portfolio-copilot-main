import json
import logging

from flask import Blueprint, jsonify, request

from api import context
from utils.helper import extract_status_label_from_value, is_in_transfer_status
from utils.update_cache import mark_update_made as _mark_update_made


logger = logging.getLogger(__name__)
bp = Blueprint("transfer", __name__)

# ==================== IN TRANSFER ENDPOINT ====================

@bp.route('/api/transfer-to-sprint', methods=['POST'])
def transfer_item_to_sprint():
    """
    When an item's status is 'In Transfer', move it to the latest sprint
    by updating the Sprint board_relation column on that item.

    Expected POST body (Monday.com webhook format):
    {
        "event": {
            "pulseId": "123456789",
            "boardId": "987654321",
            "columnId": "status",
            "value": {...}
        }
    }
    """
    logger.info(f"[transfer-to-sprint] raw body: {request.get_data(as_text=True)}")
    raw = request.get_json(silent=True) or {}
    logger.info(f"[transfer-to-sprint] parsed raw: {raw}")

    if 'challenge' in raw:
        return jsonify({'challenge': raw['challenge']}), 200

    if not context.monday_api:
        return jsonify({'error': 'Monday.com API not configured', 'status': 'error'}), 500

    try:
        data = raw

        if 'data' in data and isinstance(data['data'], str):
            try:
                data = json.loads(data['data'])
            except json.JSONDecodeError:
                pass

        event = data.get('event', data)
        logger.info(f"[transfer-to-sprint] raw data: {data}")

        item_id   = str(event.get('pulseId') or event.get('itemId') or event.get('item_id', ''))
        board_id  = str(event.get('boardId') or event.get('board_id', ''))
        column_id = str(event.get('columnId') or event.get('column_id', ''))
        new_value = event.get('value', {})

        if not item_id or not board_id:
            logger.error(f"[transfer-to-sprint] Missing required fields. pulseId={item_id}, boardId={board_id}, columnId={column_id}")
            return jsonify({
                'error': 'Missing required fields: pulseId/itemId, boardId',
                'status': 'error'
            }), 400

        logger.info(f"[transfer-to-sprint] item={item_id}, board={board_id}, column={column_id}")

        # ── Loop prevention ──────────────────────────────────────────────────
        # if _is_self_triggered_update(item_id, column_id):
        #     logger.info(f"[transfer-to-sprint] Skipping self-triggered update for {item_id}/{column_id}")
        #     return jsonify({'status': 'skipped', 'message': 'Self-triggered update ignored'}), 200

        # ── Check status label ───────────────────────────────────────────────
        # column_change events: status is in event.value
        # create_pulse events:  status is in event.columnValues.<col_id>.label.text
        status_label = extract_status_label_from_value(new_value)

        if not status_label:
            for col_data in event.get('columnValues', {}).values():
                if isinstance(col_data, dict) and 'label' in col_data:
                    label = col_data['label']
                    status_label = label.get('text', '') if isinstance(label, dict) else str(label)
                    if status_label:
                        break

        logger.info(f"[transfer-to-sprint] Status changed to: '{status_label}'")

        if not is_in_transfer_status(status_label):
            return jsonify({
                'status': 'skipped',
                'message': f"Status '{status_label}' is not 'In Transfer' – nothing to do"
            }), 200

        # ── Find the latest sprint ───────────────────────────────────────────
        source_board = context.monday_api.get_board_by_id(board_id)
        workspace_id = str(source_board.get('workspace', {}).get('id', '')) if source_board else ''

        sprints_board = context.monday_api.get_sprints_board(workspace_id=workspace_id if workspace_id else None)
        if not sprints_board:
            return jsonify({'error': 'Sprints board not found', 'status': 'error'}), 404

        latest_sprint = context.monday_api.get_latest_sprint(sprints_board['id'])
        if not latest_sprint:
            return jsonify({'error': 'No latest sprint found', 'status': 'error'}), 404

        logger.info(f"[transfer-to-sprint] Latest sprint: {latest_sprint['name']} (ID: {latest_sprint['id']})")

        # ── Find the Sprint column and update it on the item ─────────────────
        board_columns = context.monday_api.get_board_columns(board_id)
        sprint_col_id = None
        for col in board_columns:
            if col.get('type') == 'board_relation' and 'sprint' in col.get('title', '').lower():
                sprint_col_id = col['id']
                break

        if not sprint_col_id:
            logger.warning(f"[transfer-to-sprint] No Sprint board_relation column found on board {board_id}")
            return jsonify({'error': 'Sprint column not found on board', 'status': 'error'}), 404

        sprint_value = json.dumps({"item_ids": [int(latest_sprint['id'])]})
        sprint_updated = context.monday_api.update_column_value(board_id, item_id, sprint_col_id, sprint_value)
        if sprint_updated:
            _mark_update_made(item_id, sprint_col_id)

        logger.info(f"[transfer-to-sprint] Sprint column '{sprint_col_id}' update on {item_id}: {sprint_updated}")

        return jsonify({
            'status': 'success',
            'message': f"Moved item {item_id} to sprint '{latest_sprint['name']}'",
            'item_id': item_id,
            'sprint_id': latest_sprint['id'],
            'sprint_name': latest_sprint['name'],
            'sprint_column_updated': sprint_updated,
        }), 200

    except Exception as e:
        logger.error(f"[transfer-to-sprint] Error: {str(e)}", exc_info=True)
        return jsonify({'error': str(e), 'status': 'error'}), 500
