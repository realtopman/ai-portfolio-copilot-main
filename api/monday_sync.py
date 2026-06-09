import json
import logging

from flask import Blueprint, jsonify, request

from api import context
from utils.helper import (
    extract_linked_item_ids,
    extract_status_label_from_value,
    find_matching_column,
    find_matching_subitem,
    get_column_value_for_sync,
    get_link_column_id,
    identify_board_type,
    is_ir_sprint_status,
    is_move_to_sprints_status,
)
from utils.hours import calculate_hours_from_subitems, find_hours_column_id, is_hours_column
from utils.update_cache import is_self_triggered_update as _is_self_triggered_update
from utils.update_cache import mark_update_made as _mark_update_made


logger = logging.getLogger(__name__)
bp = Blueprint("monday_sync", __name__)

# ==================== COLUMN SYNC ENDPOINTS ====================

@bp.route('/api/sync-item', methods=['POST'])
def sync_item_columns():
    """
    Sync column values between linked items across boards.
    
    This endpoint handles synchronization between:
    - Fast Lane (FL) <-> Sprint Backlog
    - Bugs Queue (BQ) <-> Sprint Backlog
    
    When a column value changes on an item, this endpoint:
    1. Identifies the source board type
    2. Finds the linked item in the corresponding board
    3. Updates the matching column on the linked item
    
    Expected POST body:
    {
        "event": {
            "type": "column_change",
            "pulseId": "123456789",         # Item ID that changed
            "boardId": "987654321",         # Board ID where change occurred
            "columnId": "status",           # Column ID that changed
            "value": {...},                 # New column value (JSON)
            "previousValue": {...}          # Previous column value (optional)
        }
    }
    
    Returns:
        JSON response with sync status and details
    """
    
    data  = request.get_json()
    if 'challenge' in data:
        return jsonify({'challenge': data['challenge']}), 200
    if not context.monday_api:
        return jsonify({'error': 'Monday.com API not configured', 'status': 'error'}), 500
    
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'No data provided', 'status': 'error'}), 400
        
        logger.info(f"Received sync-item raw data: {data}")
        
        # Handle nested JSON string format: {'data': '{"event":{...}}'}
        if 'data' in data and isinstance(data['data'], str):
            try:
                parsed_data = json.loads(data['data'])
                data = parsed_data
            except json.JSONDecodeError:
                pass
        
        # Extract event details - support multiple formats
        event = data.get('event', data)
        
        item_id = str(event.get('pulseId') or event.get('itemId') or event.get('item_id', ''))
        board_id = str(event.get('boardId') or event.get('board_id', ''))
        column_id = event.get('columnId') or event.get('column_id', '')
        new_value = event.get('value', {})
        
        # Loop prevention: Check if this webhook is triggered by our own update
        # if item_id and column_id and _is_self_triggered_update(item_id, str(column_id)):
        #     logger.info(f"Skipping sync-item for {item_id}/{column_id} - self-triggered update (loop prevention)")
        #     return jsonify({
        #         'status': 'skipped',
        #         'message': 'Skipped self-triggered update to prevent loop'
        #     }), 200
        
        if not item_id or not board_id or not column_id:
            logger.error(f"Missing required fields. pulseId={item_id}, boardId={board_id}, columnId={column_id}")
            return jsonify({
                'error': 'Missing required fields: pulseId/itemId, boardId, columnId',
                'status': 'error'
            }), 400
        
        logger.info(f"Sync request: item={item_id}, board={board_id}, column={column_id}")
        
        # Get source board info
        source_board = context.monday_api.get_board_by_id(board_id)
        if not source_board:
            return jsonify({'error': f'Board {board_id} not found', 'status': 'error'}), 404
        
        source_board_name = source_board.get('name', '')
        source_board_type = identify_board_type(source_board_name)
        
        logger.info(f"Source board: {source_board_name} (type: {source_board_type})")
        
        if source_board_type == 'unknown':
            return jsonify({
                'status': 'skipped',
                'message': f'Board "{source_board_name}" is not a syncable board type (FL, BQ, or User Stories)'
            }), 200
        
        # Get source item with all columns
        source_item = context.monday_api.get_item_with_columns(item_id)
        if not source_item:
            return jsonify({'error': f'Item {item_id} not found', 'status': 'error'}), 404
        
        # Get source board columns
        source_columns = context.monday_api.get_board_columns(board_id)
        
        # ==================== SPECIAL CASE: "Move to Sprints" / "Sprint" Status ====================
        # When Status column is changed to "Move to 'Sprints'" in FL/BQ boards,
        # OR changed to "Sprint" in the Incoming Request board,
        # create a new item in User Stories board instead of syncing
        if source_board_type in ['fast_lane', 'bugs_queue', 'incoming_request']:
            # Check if this is a status column change
            source_col_info = None
            for col in source_columns:
                if col.get('id') == column_id:
                    source_col_info = col
                    break
            
            if source_col_info and source_col_info.get('type') in ['status', 'color']:
                # Extract the status label from the new value
                status_label = extract_status_label_from_value(new_value)
                logger.info(f"Status column changed to: '{status_label}'")
                
                # Determine if this status should trigger item creation in User Stories
                if source_board_type == 'incoming_request':
                    should_create = is_ir_sprint_status(status_label)
                else:
                    should_create = is_move_to_sprints_status(status_label)
                
                if should_create:
                    logger.info(f"Detected 'Move to Sprints' status - checking for existing linked item first")
                    
                    # Get workspace ID from source board
                    workspace_id = source_board.get('workspace', {}).get('id')
                    
                    # First, check if there's already a linked item in User Stories board
                    link_column_id = get_link_column_id(source_columns, source_board_type, 'user_stories')
                    existing_linked_ids = []
                    
                    if link_column_id:
                        existing_linked_ids = extract_linked_item_ids(
                            source_item.get('column_values', []),
                            link_column_id
                        )
                    
                    if existing_linked_ids:
                        # There's already a linked item - just update its status
                        logger.info(f"Found existing linked item(s): {existing_linked_ids} - updating status instead of creating new item")
                        
                        sync_results = []
                        for linked_item_id in existing_linked_ids:
                            try:
                                linked_item = context.monday_api.get_item_with_columns(linked_item_id)
                                if not linked_item:
                                    logger.warning(f"Linked item {linked_item_id} not found")
                                    continue
                                
                                linked_board_id = linked_item.get('board', {}).get('id')
                                if not linked_board_id:
                                    continue
                                
                                # Get target board columns
                                target_columns = context.monday_api.get_board_columns(linked_board_id)
                                
                                # Find matching status column
                                matching_column = find_matching_column(source_columns, target_columns, column_id)
                                
                                if matching_column:
                                    # Update the status on the linked item
                                    value_to_sync = json.dumps(new_value) if isinstance(new_value, dict) else new_value
                                    success = context.monday_api.update_column_value(
                                        linked_board_id,
                                        linked_item_id,
                                        matching_column['id'],
                                        value_to_sync
                                    )
                                    
                                    sync_results.append({
                                        'linked_item_id': linked_item_id,
                                        'linked_item_name': linked_item.get('name', ''),
                                        'target_board_id': linked_board_id,
                                        'target_column_id': matching_column['id'],
                                        'target_column_title': matching_column['title'],
                                        'success': success
                                    })
                                    
                                    logger.info(f"Updated status on linked item {linked_item_id}: {success}")
                                else:
                                    logger.info(f"No matching status column found in linked item's board")
                                    
                            except Exception as e:
                                logger.error(f"Error updating linked item {linked_item_id}: {str(e)}")
                                sync_results.append({
                                    'linked_item_id': linked_item_id,
                                    'error': str(e),
                                    'success': False
                                })
                        
                        successful_syncs = sum(1 for r in sync_results if r.get('success'))
                        return jsonify({
                            'status': 'success',
                            'action': 'updated_existing_linked_item',
                            'message': f'Updated status on {successful_syncs} existing linked item(s)',
                            'source': {
                                'item_id': item_id,
                                'item_name': source_item.get('name', ''),
                                'board_id': board_id,
                                'board_name': source_board_name,
                                'board_type': source_board_type,
                                'status_value': status_label
                            },
                            'sync_results': sync_results
                        }), 200
                    
                    # No linked item exists - create new item in User Stories board
                    logger.info(f"No linked item found - creating new item in User Stories board")
                    
                    # Find User Stories board
                    user_stories_board = context.monday_api.get_user_stories_board(workspace_id)
                    if not user_stories_board:
                        return jsonify({
                            'error': 'Sprint Backlog board not found',
                            'status': 'error'
                        }), 404
                    
                    # Get active sprint
                    active_sprint = context.monday_api.get_active_sprint_for_user_stories(workspace_id)
                    if not active_sprint:
                        logger.warning("No active sprint found - item will be created without sprint assignment")
                    
                    # Get User Stories board columns for mapping
                    us_columns = context.monday_api.get_board_columns(user_stories_board['id'])
                    
                    # Build column values for the new item
                    new_item_column_values = {}
                    skipped_columns = []
                    
                    logger.info(f"Processing {len(source_item.get('column_values', []))} source columns for mapping")
                    
                    # Build a lookup dict from source_columns list
                    source_columns_dict = {col.get('id'): col for col in source_columns} if isinstance(source_columns, list) else source_columns
                    
                    # Debug: log all source columns (especially board_relation ones for Epic)
                    for cv in source_item.get('column_values', []):
                        cv_type = cv.get('type', '')
                        cv_title = source_columns_dict.get(cv.get('id'), {}).get('title', cv.get('id'))
                        if cv_type == 'board_relation':
                            logger.info(f"  Source board_relation column '{cv_title}' ({cv.get('id')}): value={cv.get('value', '')[:100] if cv.get('value') else 'None'}, linked_item_ids={cv.get('linked_item_ids', [])}")
                    
                    # Map columns from source to User Stories board
                    for source_cv in source_item.get('column_values', []):
                        source_col_id = source_cv.get('id')
                        source_value = source_cv.get('value')
                        source_text = source_cv.get('text', '')  # Also get text representation
                        source_type = source_cv.get('type', '')
                        source_linked_ids = source_cv.get('linked_item_ids', [])  # For board_relation columns
                        
                        # Debug: log status/color columns to see their format
                        if source_type in ['status', 'color'] and source_value and source_value != 'null':
                            logger.info(f"  Source status column '{source_col_id}': value={source_value[:100] if source_value else 'None'}, text='{source_text}'")
                        
                        # Skip empty values, but allow board_relation with linked_item_ids
                        if (not source_value or source_value == 'null') and not source_linked_ids:
                            continue
                        
                        # Find matching column in User Stories board
                        matching_col = find_matching_column(source_columns, us_columns, source_col_id)
                        
                        if matching_col:
                            target_col_id = matching_col['id']
                            target_col_type = matching_col.get('type', '')
                            target_col_title = matching_col.get('title', '')
                            
                            # Skip column types that cannot be set via API
                            # Mirror/lookup columns are read-only (they pull from linked items)
                            # Formula, auto_number, creation_log, last_updated are system columns
                            unsupported_types = ['mirror', 'lookup', 'formula', 'auto_number', 
                                                 'creation_log', 'last_updated', 'item_id', 
                                                 'subtasks', 'dependency', 'direct_doc']
                            if target_col_type in unsupported_types:
                                skipped_columns.append(f"{target_col_title} ({target_col_type})")
                                continue
                            
                            # Handle different column types
                            try:
                                # Parse value, but handle None for board_relation columns with linked_item_ids
                                parsed_value = None
                                if source_value and source_value != 'null':
                                    parsed_value = json.loads(source_value) if isinstance(source_value, str) else source_value
                                
                                # For status columns, use label text
                                # The 'value' often only has index, but 'text' has the display label
                                if target_col_type in ['status', 'color']:
                                    label_text = extract_status_label_from_value(parsed_value)
                                    logger.info(f"    Status '{target_col_title}': extract_status_label returned '{label_text}', source_text='{source_text}'")
                                    # If no label text in value, use the text field from column_values
                                    if not label_text and source_text:
                                        label_text = source_text
                                    if label_text:
                                        new_item_column_values[target_col_id] = {"label": label_text}
                                        logger.info(f"  Mapped status '{target_col_title}': '{label_text}'")
                                    else:
                                        logger.warning(f"  Could not extract label for status '{target_col_title}'")
                                
                                # For people columns, keep the format
                                elif target_col_type == 'people':
                                    if isinstance(parsed_value, dict) and 'personsAndTeams' in parsed_value:
                                        new_item_column_values[target_col_id] = parsed_value
                                        logger.info(f"  Mapped people '{target_col_title}': {source_text}")
                                    elif isinstance(parsed_value, dict) and ('id' in parsed_value or 'kind' in parsed_value):
                                        # Direct person reference
                                        new_item_column_values[target_col_id] = parsed_value
                                        logger.info(f"  Mapped people '{target_col_title}': {source_text}")
                                
                                # For numeric columns, extract value
                                elif target_col_type in ['numeric', 'numbers']:
                                    numeric_val = None
                                    if isinstance(parsed_value, dict):
                                        if 'value' in parsed_value:
                                            numeric_val = parsed_value['value']
                                        elif 'number' in parsed_value:
                                            numeric_val = parsed_value['number']
                                    elif isinstance(parsed_value, (int, float)):
                                        numeric_val = parsed_value
                                    elif isinstance(parsed_value, str):
                                        # Value might be a string number like "8"
                                        try:
                                            numeric_val = float(parsed_value.replace(',', ''))
                                        except:
                                            pass
                                    
                                    # Try parsing from text field if still no value
                                    if numeric_val is None and source_text:
                                        try:
                                            numeric_val = float(source_text.replace(',', ''))
                                        except:
                                            pass
                                    
                                    if numeric_val is not None:
                                        new_item_column_values[target_col_id] = str(numeric_val)
                                        logger.info(f"  Mapped numeric '{target_col_title}': {numeric_val}")
                                
                                # For dropdown columns
                                elif target_col_type == 'dropdown':
                                    if isinstance(parsed_value, dict) and 'labels' in parsed_value:
                                        new_item_column_values[target_col_id] = parsed_value
                                        logger.info(f"  Mapped dropdown '{target_col_title}': {source_text}")
                                    elif isinstance(parsed_value, dict) and 'ids' in parsed_value:
                                        new_item_column_values[target_col_id] = parsed_value
                                        logger.info(f"  Mapped dropdown '{target_col_title}': {source_text}")
                                
                                # For text columns
                                elif target_col_type in ['text', 'long_text']:
                                    text_val = None
                                    if isinstance(parsed_value, str):
                                        text_val = parsed_value
                                    elif isinstance(parsed_value, dict):
                                        text_val = parsed_value.get('text') or parsed_value.get('value') or source_text
                                    
                                    if text_val:
                                        new_item_column_values[target_col_id] = text_val
                                        logger.info(f"  Mapped text '{target_col_title}': '{text_val[:50]}...' " if len(str(text_val)) > 50 else f"  Mapped text '{target_col_title}': '{text_val}'")
                                
                                # For link columns
                                elif target_col_type == 'link':
                                    if isinstance(parsed_value, dict) and ('url' in parsed_value or 'text' in parsed_value):
                                        new_item_column_values[target_col_id] = parsed_value
                                        logger.info(f"  Mapped link '{target_col_title}': {source_text}")
                                
                                # For date columns
                                elif target_col_type == 'date':
                                    if isinstance(parsed_value, dict) and 'date' in parsed_value:
                                        new_item_column_values[target_col_id] = parsed_value
                                        logger.info(f"  Mapped date '{target_col_title}': {source_text}")
                                
                                # For timeline columns
                                elif target_col_type == 'timeline':
                                    if isinstance(parsed_value, dict) and 'from' in parsed_value:
                                        new_item_column_values[target_col_id] = parsed_value
                                        logger.info(f"  Mapped timeline '{target_col_title}': {source_text}")
                                
                                # For checkbox columns
                                elif target_col_type == 'checkbox':
                                    if isinstance(parsed_value, dict) and 'checked' in parsed_value:
                                        new_item_column_values[target_col_id] = parsed_value
                                        logger.info(f"  Mapped checkbox '{target_col_title}': {parsed_value.get('checked')}")
                                
                                # For board relation (link) columns - handle Epics and other relations
                                elif target_col_type == 'board_relation':
                                    # Check if this is an Epic or other linkable column (not FL/BQ/Sprint links)
                                    target_title_lower = target_col_title.lower()
                                    skip_patterns = ['fast lane', 'bugs queue', 'sprint', 'fl', 'bq']
                                    should_sync = any(p in target_title_lower for p in ['epic', 'connected', 'linked', 'parent', 'depend'])
                                    should_skip = any(p in target_title_lower for p in skip_patterns)
                                    
                                    if should_sync and not should_skip:
                                        # Get linked item IDs - prefer source_linked_ids from GraphQL response
                                        linked_ids = source_linked_ids if source_linked_ids else []
                                        
                                        # Fallback to parsing from value
                                        if not linked_ids and parsed_value:
                                            if isinstance(parsed_value, dict) and 'linkedPulseIds' in parsed_value:
                                                linked_ids = [p.get('linkedPulseId') for p in parsed_value.get('linkedPulseIds', []) if p.get('linkedPulseId')]
                                            elif isinstance(parsed_value, dict) and 'item_ids' in parsed_value:
                                                linked_ids = parsed_value.get('item_ids', [])
                                        
                                        if linked_ids:
                                            new_item_column_values[target_col_id] = {"item_ids": [int(lid) for lid in linked_ids]}
                                            logger.info(f"  Mapped board_relation '{target_col_title}': {linked_ids}")
                                
                                # For duration columns (like "Duration in Status")
                                elif target_col_type == 'duration':
                                    if parsed_value and isinstance(parsed_value, dict):
                                        new_item_column_values[target_col_id] = parsed_value
                                        logger.info(f"  Mapped duration '{target_col_title}': {source_text}")
                                
                                else:
                                    # For other types, try to use the value directly
                                    if parsed_value is not None:
                                        new_item_column_values[target_col_id] = parsed_value
                                    logger.info(f"  Mapped other '{target_col_title}' ({target_col_type}): {source_text}")
                                    
                            except (json.JSONDecodeError, TypeError) as e:
                                logger.warning(f"Could not parse value for column {source_col_id}: {e}")
                    
                    if skipped_columns:
                        logger.info(f"Skipped unsupported columns: {', '.join(skipped_columns)}")
                    
                    logger.info(f"Built {len(new_item_column_values)} column values for new item")
                    
                    # Set Sprint column to active sprint
                    if active_sprint:
                        # Find Sprint column in User Stories board
                        for col in us_columns:
                            col_title = col.get('title', '').lower()
                            if 'sprint' in col_title and col.get('type') == 'board_relation':
                                new_item_column_values[col['id']] = {"item_ids": [int(active_sprint['id'])]}
                                logger.info(f"Setting Sprint column to: {active_sprint['name']} (ID: {active_sprint['id']})")
                                break
                    
                    # Get the first group or find appropriate group
                    target_group_id = None
                    if user_stories_board.get('groups'):
                        target_group_id = user_stories_board['groups'][0]['id']
                    else:
                        target_group_id = 'topics'  # Default group
                    
                    # Log the column values being sent (for debugging)
                    logger.info(f"Final column values to set: {json.dumps(new_item_column_values, default=str)[:500]}...")
                    
                    # Create the item in User Stories board
                    source_item_name = source_item.get('name', 'Untitled')
                    new_item_id = context.monday_api.create_item_with_columns(
                        board_id=user_stories_board['id'],
                        group_id=target_group_id,
                        item_name=source_item_name,
                        column_values=new_item_column_values,
                        create_labels_if_missing=True
                    )
                    
                    if not new_item_id:
                        return jsonify({
                            'error': 'Failed to create item in User Stories board',
                            'status': 'error'
                        }), 500
                    
                    logger.info(f"Created item '{source_item_name}' in User Stories board (ID: {new_item_id})")
                    
                    # Copy subitems from source item to new item
                    source_subitems = context.monday_api.get_item_subitems(item_id)
                    created_subitems = []
                    
                    if source_subitems:
                        logger.info(f"Copying {len(source_subitems)} subitems to new item")
                        
                        for subitem in source_subitems:
                            subitem_name = subitem.get('name', 'Untitled Subitem')
                            
                            # Get source subitem board columns
                            source_subitem_board_id = subitem.get('board', {}).get('id')
                            source_subitem_columns = context.monday_api.get_board_columns(source_subitem_board_id) if source_subitem_board_id else []
                            
                            # Create the subitem first (we'll update columns after)
                            new_subitem_id = context.monday_api.create_subitem(
                                parent_item_id=new_item_id,
                                subitem_name=subitem_name,
                                column_values=None  # Create without values first
                            )
                            
                            if new_subitem_id:
                                created_subitems.append({
                                    'id': new_subitem_id,
                                    'name': subitem_name
                                })
                                
                                # Now get the target subitem board columns and sync values
                                new_subitem_info = context.monday_api.get_subitem_with_all_columns(new_subitem_id)
                                if new_subitem_info:
                                    target_subitem_board_id = new_subitem_info.get('board', {}).get('id')
                                    target_subitem_columns = context.monday_api.get_board_columns(target_subitem_board_id) if target_subitem_board_id else []
                                    
                                    synced_subitem_cols = 0
                                    # Sync each column value
                                    for src_cv in subitem.get('column_values', []):
                                        src_col_id = src_cv.get('id')
                                        src_value = src_cv.get('value')
                                        src_text = src_cv.get('text', '')  # Display text from Monday API
                                        src_type = src_cv.get('type', '')
                                        
                                        # Debug time_tracking columns
                                        if src_type == 'time_tracking':
                                            logger.info(f"    DEBUG time_tracking column '{src_col_id}': value={src_value[:100] if src_value else 'None'}, text='{src_text}'")
                                        
                                        if not src_value or src_value == 'null':
                                            continue
                                        
                                        # Find matching column
                                        matching_col = find_matching_column(source_subitem_columns, target_subitem_columns, src_col_id)
                                        
                                        if matching_col:
                                            target_col_type = matching_col.get('type', '')
                                            
                                            # Skip unsupported column types for subitems too
                                            if target_col_type in ['mirror', 'lookup', 'formula', 'auto_number', 
                                                                   'creation_log', 'last_updated', 'item_id']:
                                                continue
                                            
                                            try:
                                                # Format the value appropriately based on column type
                                                parsed_value = json.loads(src_value) if isinstance(src_value, str) else src_value
                                                value_to_set = None  # Will be set based on type
                                                
                                                # For status columns, use label text
                                                # The 'value' often only has index, but 'text' has the display label
                                                if target_col_type in ['status', 'color']:
                                                    label_text = extract_status_label_from_value(parsed_value)
                                                    # If no label in value, use the text field
                                                    if not label_text and src_text:
                                                        label_text = src_text
                                                    if label_text:
                                                        value_to_set = json.dumps({"label": label_text})
                                                        logger.info(f"    Subitem status '{matching_col.get('title')}': '{label_text}'")
                                                
                                                # For numeric columns
                                                elif target_col_type in ['numeric', 'numbers']:
                                                    numeric_val = None
                                                    if isinstance(parsed_value, dict) and 'value' in parsed_value:
                                                        numeric_val = parsed_value['value']
                                                    elif isinstance(parsed_value, (int, float)):
                                                        numeric_val = parsed_value
                                                    elif isinstance(parsed_value, str):
                                                        # Value might be a string number like "8"
                                                        try:
                                                            numeric_val = float(parsed_value.replace(',', ''))
                                                        except:
                                                            pass
                                                    # Try to parse from text field
                                                    if numeric_val is None and src_text:
                                                        try:
                                                            numeric_val = float(src_text.replace(',', ''))
                                                        except:
                                                            pass
                                                    if numeric_val is not None:
                                                        value_to_set = str(numeric_val)
                                                        logger.info(f"    Subitem numeric '{matching_col.get('title')}': {numeric_val}")
                                                
                                                # For people columns
                                                elif target_col_type == 'people':
                                                    if isinstance(parsed_value, dict) and 'personsAndTeams' in parsed_value:
                                                        value_to_set = json.dumps(parsed_value)
                                                        logger.info(f"    Subitem people '{matching_col.get('title')}': {src_text}")
                                                
                                                # For text columns
                                                elif target_col_type in ['text', 'long_text']:
                                                    if src_text:
                                                        value_to_set = src_text
                                                        logger.info(f"    Subitem text '{matching_col.get('title')}': '{src_text[:30]}...' " if len(src_text) > 30 else f"    Subitem text '{matching_col.get('title')}': '{src_text}'")
                                                
                                                # For date columns
                                                elif target_col_type == 'date':
                                                    if isinstance(parsed_value, dict) and 'date' in parsed_value:
                                                        value_to_set = json.dumps(parsed_value)
                                                        logger.info(f"    Subitem date '{matching_col.get('title')}': {src_text}")
                                                
                                                # For link columns
                                                elif target_col_type == 'link':
                                                    if isinstance(parsed_value, dict) and 'url' in parsed_value:
                                                        value_to_set = json.dumps(parsed_value)
                                                        logger.info(f"    Subitem link '{matching_col.get('title')}': {src_text}")
                                                
                                                # For duration/time_tracking columns (like "Duration in Status")
                                                elif target_col_type in ['duration', 'time_tracking']:
                                                    if isinstance(parsed_value, dict):
                                                        value_to_set = json.dumps(parsed_value)
                                                        logger.info(f"    Subitem duration '{matching_col.get('title')}': {src_text}")
                                                    elif src_text:
                                                        # Duration might be in text format - but time_tracking needs dict format
                                                        # Try to convert "0m 18s" or "1h 0m 0s" to the expected format
                                                        logger.info(f"    Subitem duration '{matching_col.get('title')}': {src_text} (text only, may need special handling)")
                                                
                                                if value_to_set:
                                                    context.monday_api.update_column_value(
                                                        target_subitem_board_id,
                                                        new_subitem_id,
                                                        matching_col['id'],
                                                        value_to_set,
                                                        target_col_type
                                                    )
                                                    synced_subitem_cols += 1
                                            except Exception as e:
                                                logger.warning(f"Failed to sync subitem column {matching_col.get('title', src_col_id)}: {e}")
                                    
                                    logger.info(f"  Created subitem '{subitem_name}' (ID: {new_subitem_id}) - synced {synced_subitem_cols} columns")
                                else:
                                    logger.info(f"  Created subitem '{subitem_name}' (ID: {new_subitem_id})")
                            else:
                                logger.warning(f"Failed to create subitem '{subitem_name}'")
                    
                    # Link the new User Stories item back to the source FL/BQ item
                    # Find the appropriate link column in User Stories board
                    link_col_patterns = []
                    if source_board_type == 'fast_lane':
                        link_col_patterns = ['fast lane', 'fl']
                    elif source_board_type == 'bugs_queue':
                        link_col_patterns = ['bugs queue', 'bq', 'bug']
                    elif source_board_type == 'incoming_request':
                        link_col_patterns = ['incoming request', 'ir', 'incoming requests']
                    
                    for col in us_columns:
                        col_title = col.get('title', '').lower()
                        col_type = col.get('type', '')
                        
                        if col_type == 'board_relation':
                            for pattern in link_col_patterns:
                                if pattern in col_title:
                                    # Link to source item
                                    try:
                                        link_value = json.dumps({"item_ids": [int(item_id)]})
                                        context.monday_api.update_column_value(
                                            user_stories_board['id'],
                                            new_item_id,
                                            col['id'],
                                            link_value
                                        )
                                        logger.info(f"Linked new item to source {source_board_type} item {item_id}")
                                    except Exception as e:
                                        logger.warning(f"Failed to link to source item: {e}")
                                    break
                    
                    # Also update the source FL/BQ item to link to the new User Stories item
                    if link_column_id:
                        try:
                            source_link_value = json.dumps({"item_ids": [int(new_item_id)]})
                            context.monday_api.update_column_value(
                                board_id,
                                item_id,
                                link_column_id,
                                source_link_value
                            )
                            logger.info(f"Updated source item {item_id} to link to new User Stories item {new_item_id}")
                        except Exception as e:
                            logger.warning(f"Failed to update source item link: {e}")
                    
                    return jsonify({
                        'status': 'success',
                        'action': 'created_in_user_stories',
                        'message': f'Created item in User Stories board from {source_board_type}',
                        'source': {
                            'item_id': item_id,
                            'item_name': source_item_name,
                            'board_id': board_id,
                            'board_name': source_board_name,
                            'board_type': source_board_type,
                            'status_value': status_label
                        },
                        'created_item': {
                            'item_id': new_item_id,
                            'board_id': user_stories_board['id'],
                            'board_name': user_stories_board['name'],
                            'sprint': active_sprint['name'] if active_sprint else None,
                            'subitems_created': len(created_subitems)
                        }
                    }), 200
        
        # ==================== END SPECIAL CASE ====================
        
        # ==================== HOURS COLUMN RECALCULATION ====================
        # When Estimated Hrs or Actual Hrs column changes on an item:
        # - If item has subitems, recalculate from subitem sum and use that value
        # - Then sync the (recalculated) value to linked items
        
        # Get the column info for the changed column
        changed_column_info = None
        for col in source_columns:
            if col.get('id') == column_id:
                changed_column_info = col
                break
        
        value_to_sync_override = None  # Will be set if we need to override the value
        
        if changed_column_info:
            column_title = changed_column_info.get('title', '')
            hours_type = is_hours_column(column_title)
            
            if hours_type:
                logger.info(f"Detected hours column change: '{column_title}' ({hours_type})")
                
                # Check if item has subitems
                subitems = context.monday_api.get_item_subitems(item_id)
                
                if subitems and len(subitems) > 0:
                    logger.info(f"Item has {len(subitems)} subitems - recalculating {hours_type} hours from sum")
                    
                    # Calculate sum from subitems
                    calculated_sum = calculate_hours_from_subitems(subitems, column_title)
                    logger.info(f"Calculated {hours_type} hours from subitems: {calculated_sum}")
                    
                    # Update the source item with the calculated sum
                    success = context.monday_api.update_column_value(
                        board_id,
                        item_id,
                        column_id,
                        str(calculated_sum),
                        'numeric'
                    )
                    if success:
                        _mark_update_made(item_id, column_id)
                    logger.info(f"Reset item's {column_title} to subitem sum {calculated_sum}: {success}")
                    
                    # Override the sync value with the calculated sum
                    value_to_sync_override = str(calculated_sum)
                else:
                    logger.info(f"Item has no subitems - using provided value directly")
        
        # ==================== END HOURS RECALCULATION ====================
        
        # Determine target board type(s)
        target_board_types = []
        if source_board_type == 'user_stories':
            # User Stories can link to FL, BQ, and Incoming Request
            target_board_types = ['fast_lane', 'bugs_queue', 'incoming_request']
        else:
            # FL, BQ, and IR all link to User Stories
            target_board_types = ['user_stories']
        
        sync_results = []
        
        for target_board_type in target_board_types:
            # Find the link column that connects to target board type
            link_column_id = get_link_column_id(source_columns, source_board_type, target_board_type)
            
            if not link_column_id:
                logger.info(f"No link column found for {source_board_type} -> {target_board_type}")
                continue
            
            # Extract linked item IDs
            linked_item_ids = extract_linked_item_ids(
                source_item.get('column_values', []),
                link_column_id
            )
            
            if not linked_item_ids:
                logger.info(f"No linked items found in column {link_column_id}")
                continue
            
            # Process each linked item
            for linked_item_id in linked_item_ids:
                try:
                    # Get linked item details
                    linked_item = context.monday_api.get_item_with_columns(linked_item_id)
                    if not linked_item:
                        logger.warning(f"Linked item {linked_item_id} not found")
                        continue
                    
                    linked_board_id = linked_item.get('board', {}).get('id')
                    if not linked_board_id:
                        logger.warning(f"Could not get board ID for linked item {linked_item_id}")
                        continue
                    
                    # Get target board columns
                    target_columns = context.monday_api.get_board_columns(linked_board_id)
                    
                    # Find matching column in target board
                    matching_column = find_matching_column(source_columns, target_columns, column_id)
                    
                    if not matching_column:
                        logger.info(f"No matching column for {column_id} in target board {linked_board_id}")
                        continue
                    
                    # Get the value to sync - use override if set (for hours recalculation)
                    if value_to_sync_override is not None:
                        value_to_sync = value_to_sync_override
                        logger.info(f"Using recalculated hours value: {value_to_sync}")
                    elif isinstance(new_value, dict):
                        value_to_sync = json.dumps(new_value)
                    elif isinstance(new_value, str):
                        # Check if it's already valid JSON
                        try:
                            json.loads(new_value)
                            value_to_sync = new_value
                        except:
                            # Get value from source item's column
                            col_value = get_column_value_for_sync(
                                source_item.get('column_values', []),
                                column_id
                            )
                            if col_value:
                                value_to_sync = col_value.get('value', '{}')
                            else:
                                value_to_sync = json.dumps({})
                    else:
                        value_to_sync = json.dumps({})
                    
                    # Update the linked item's column
                    success = context.monday_api.update_column_value(
                        linked_board_id,
                        linked_item_id,
                        matching_column['id'],
                        value_to_sync
                    )
                    
                    if success:
                        _mark_update_made(linked_item_id, matching_column['id'])
                    
                    sync_results.append({
                        'linked_item_id': linked_item_id,
                        'linked_item_name': linked_item.get('name', ''),
                        'target_board_id': linked_board_id,
                        'target_column_id': matching_column['id'],
                        'target_column_title': matching_column['title'],
                        'success': success
                    })
                    
                    logger.info(f"Synced column {column_id} -> {matching_column['id']} for item {linked_item_id}: {success}")
                    
                except Exception as e:
                    logger.error(f"Error syncing to linked item {linked_item_id}: {str(e)}")
                    sync_results.append({
                        'linked_item_id': linked_item_id,
                        'error': str(e),
                        'success': False
                    })
        
        # Build response
        successful_syncs = sum(1 for r in sync_results if r.get('success'))
        
        return jsonify({
            'status': 'success',
            'message': f'Synced {successful_syncs} of {len(sync_results)} linked items',
            'source': {
                'item_id': item_id,
                'item_name': source_item.get('name', ''),
                'board_id': board_id,
                'board_name': source_board_name,
                'board_type': source_board_type,
                'column_id': column_id
            },
            'sync_results': sync_results
        }), 200
        
    except Exception as e:
        logger.error(f"Error in sync-item endpoint: {str(e)}", exc_info=True)
        return jsonify({
            'error': str(e),
            'status': 'error'
        }), 500


@bp.route('/api/sync-subitem', methods=['POST'])
def sync_subitem_columns():
    """
    Sync column values between subitems of linked parent items.
    
    When a subitem's column changes, this endpoint:
    1. Gets the parent item of the subitem
    2. Finds the linked item of the parent (in another board)
    3. Finds matching subitem in the linked item's subitems
    4. Updates the matching column on the target subitem
    
    Expected POST body:
    {
        "event": {
            "type": "column_change",
            "pulseId": "123456789",         # Subitem ID that changed
            "boardId": "987654321",         # Subitems board ID
            "columnId": "status",           # Column ID that changed
            "value": {...},                 # New column value (JSON)
            "parentItemId": "111222333"     # Optional: parent item ID
        }
    }
    
    Returns:
        JSON response with sync status and details
    """
    data = request.get_json()
    if 'challenge' in data:
        return jsonify({'challenge': data['challenge']}), 200
    if not context.monday_api:
        return jsonify({'error': 'Monday.com API not configured', 'status': 'error'}), 500
    
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'No data provided', 'status': 'error'}), 400
        
        # Handle nested JSON string format: {'data': '{"event":{...}}'}
        if 'data' in data and isinstance(data['data'], str):
            try:
                parsed_data = json.loads(data['data'])
                data = parsed_data
            except json.JSONDecodeError:
                pass
        
        # Extract event details - support multiple formats
        event = data.get('event', data)
        logger.info(f"Parsed sync-subitem event: {event}")
        
        subitem_id = str(event.get('pulseId') or event.get('itemId') or event.get('item_id', ''))
        board_id = str(event.get('boardId') or event.get('board_id', ''))
        column_id = str(event.get('columnId') or event.get('column_id', ''))
        
        # Loop prevention: Check if this webhook is triggered by our own update
        # if subitem_id and column_id and _is_self_triggered_update(subitem_id, column_id):
        #     logger.info(f"Skipping sync-subitem for {subitem_id}/{column_id} - self-triggered update (loop prevention)")
        #     return jsonify({
        #         'status': 'skipped',
        #         'message': 'Skipped self-triggered update to prevent loop'
        #     }), 200
        column_id = event.get('columnId') or event.get('column_id', '')
        new_value = event.get('value', {})
        
        # Get parent item ID from event if available (Monday.com provides this for subitems)
        event_parent_item_id = str(event.get('parentItemId') or event.get('parent_item_id', ''))
        event_parent_board_id = str(event.get('parentItemBoardId') or event.get('parent_board_id', ''))
        
        if not subitem_id or not column_id:
            logger.error(f"Missing required fields. pulseId={subitem_id}, columnId={column_id}")
            return jsonify({
                'error': 'Missing required fields: pulseId/itemId, columnId',
                'status': 'error'
            }), 400
        
        logger.info(f"Subitem sync request: subitem={subitem_id}, board={board_id}, column={column_id}, parentItemId={event_parent_item_id}")
        
        # Get subitem with parent info
        subitem = context.monday_api.get_subitem_with_parent(subitem_id)
        if not subitem:
            return jsonify({'error': f'Subitem {subitem_id} not found', 'status': 'error'}), 404
        
        # If parent_item not in subitem response, use the parentItemId from event
        if not subitem.get('parent_item') and event_parent_item_id:
            logger.info(f"Using parentItemId from event: {event_parent_item_id}")
            parent_item_from_event = context.monday_api.get_item_with_columns(event_parent_item_id)
            if parent_item_from_event:
                subitem['parent_item'] = parent_item_from_event
                # Get board name if we only have board ID
                if event_parent_board_id and not parent_item_from_event.get('board', {}).get('name'):
                    parent_board_info = context.monday_api.get_board_by_id(event_parent_board_id)
                    if parent_board_info:
                        subitem['parent_item']['board'] = parent_board_info
        
        # Get parent item
        parent_item = subitem.get('parent_item')
        if not parent_item:
            return jsonify({
                'status': 'skipped',
                'message': f'No parent item found for subitem {subitem_id}'
            }), 200
        
        parent_id = parent_item.get('id')
        parent_board_id = parent_item.get('board', {}).get('id')
        parent_board_name = parent_item.get('board', {}).get('name', '')
        
        logger.info(f"Parent item: {parent_id} on board {parent_board_name}")
        
        # Identify parent board type
        parent_board_type = identify_board_type(parent_board_name)
        
        if parent_board_type == 'unknown':
            return jsonify({
                'status': 'skipped',
                'message': f'Parent board "{parent_board_name}" is not a syncable board type'
            }), 200
        
        # Get parent board columns
        parent_columns = context.monday_api.get_board_columns(parent_board_id)
        
        # Get subitem board columns to identify the changed column
        subitem_board_id = subitem.get('board', {}).get('id') or board_id
        subitem_columns = context.monday_api.get_board_columns(subitem_board_id) if subitem_board_id else []
        
        # Find the changed column info
        changed_column_info = None
        for col in subitem_columns:
            if col.get('id') == column_id:
                changed_column_info = col
                break
        
        # ==================== SUBITEM HOURS RECALCULATION ====================
        # When a subitem's Est/Actual Hrs changes, recalculate parent's hours and sync
        parent_hours_updated = False
        parent_hours_sync_needed = []  # List of (column_id, column_title, new_value) to sync
        
        if changed_column_info:
            column_title = changed_column_info.get('title', '')
            hours_type = is_hours_column(column_title)
            
            if hours_type:
                logger.info(f"Detected subitem hours column change: '{column_title}' ({hours_type})")
                
                # Get all subitems of the parent
                all_subitems = context.monday_api.get_item_subitems(parent_id)
                
                if all_subitems:
                    # Calculate sum from all subitems
                    calculated_sum = calculate_hours_from_subitems(all_subitems, column_title)
                    logger.info(f"Calculated parent's {hours_type} hours from {len(all_subitems)} subitems: {calculated_sum}")
                    
                    # Find the corresponding column in parent board
                    parent_hours_col_id, parent_hours_col_title = find_hours_column_id(parent_columns, hours_type)
                    
                    if parent_hours_col_id:
                        # Update parent item with calculated sum
                        success = context.monday_api.update_column_value(
                            parent_board_id,
                            parent_id,
                            parent_hours_col_id,
                            str(calculated_sum),
                            'numeric'
                        )
                        logger.info(f"Updated parent item's {parent_hours_col_title} to {calculated_sum}: {success}")
                        
                        if success:
                            parent_hours_updated = True
                            parent_hours_sync_needed.append((parent_hours_col_id, parent_hours_col_title, calculated_sum))
                    else:
                        logger.warning(f"Could not find {hours_type} hours column in parent board")
        
        # ==================== END SUBITEM HOURS RECALCULATION ====================
        
        # Determine target board types
        target_board_types = []
        if parent_board_type == 'user_stories':
            target_board_types = ['fast_lane', 'bugs_queue']
        else:
            target_board_types = ['user_stories']
        
        sync_results = []
        
        for target_board_type in target_board_types:
            # Find link column in parent item's board
            link_column_id = get_link_column_id(parent_columns, parent_board_type, target_board_type)
            
            if not link_column_id:
                logger.info(f"No link column found for {parent_board_type} -> {target_board_type}")
                continue
            
            # Extract linked item IDs from parent
            linked_parent_ids = extract_linked_item_ids(
                parent_item.get('column_values', []),
                link_column_id
            )
            
            if not linked_parent_ids:
                logger.info(f"No linked items found in parent's column {link_column_id}")
                continue
            
            # Sync parent hours to linked parent items if hours were updated
            if parent_hours_updated and parent_hours_sync_needed:
                for linked_parent_id in linked_parent_ids:
                    try:
                        linked_parent = context.monday_api.get_item_with_columns(linked_parent_id)
                        if not linked_parent:
                            continue
                        
                        linked_parent_board_id = linked_parent.get('board', {}).get('id')
                        if not linked_parent_board_id:
                            continue
                        
                        linked_parent_columns = context.monday_api.get_board_columns(linked_parent_board_id)
                        
                        for parent_hours_col_id, parent_hours_col_title, hours_value in parent_hours_sync_needed:
                            # Determine hours type
                            hours_type = is_hours_column(parent_hours_col_title)
                            if not hours_type:
                                continue
                            
                            # Find matching hours column in linked parent board
                            target_hours_col_id, target_hours_col_title = find_hours_column_id(linked_parent_columns, hours_type)
                            
                            if target_hours_col_id:
                                success = context.monday_api.update_column_value(
                                    linked_parent_board_id,
                                    linked_parent_id,
                                    target_hours_col_id,
                                    str(hours_value),
                                    'numeric'
                                )
                                if success:
                                    _mark_update_made(linked_parent_id, target_hours_col_id)
                                logger.info(f"Synced parent hours {parent_hours_col_title}={hours_value} to linked parent {linked_parent_id} column {target_hours_col_title}: {success}")
                                
                                sync_results.append({
                                    'type': 'parent_hours_sync',
                                    'linked_parent_id': linked_parent_id,
                                    'column_title': target_hours_col_title,
                                    'value': hours_value,
                                    'success': success
                                })
                    except Exception as e:
                        logger.error(f"Error syncing parent hours to linked parent {linked_parent_id}: {str(e)}")
            
            # Process each linked parent item
            for linked_parent_id in linked_parent_ids:
                try:
                    # Get subitems of linked parent
                    linked_subitems = context.monday_api.get_item_subitems(linked_parent_id)
                    
                    # Find matching subitem by name
                    source_subitem_name = subitem.get('name', '')
                    target_subitem = find_matching_subitem(source_subitem_name, linked_subitems) if linked_subitems else None
                    
                    # Get source subitem board info for column mapping
                    source_subitem_board_id = subitem.get('board', {}).get('id')
                    source_subitem_columns = context.monday_api.get_board_columns(source_subitem_board_id) if source_subitem_board_id else []
                    
                    # If no matching subitem found, create it
                    if not target_subitem:
                        logger.info(f"No matching subitem found for '{source_subitem_name}' in linked parent {linked_parent_id}. Creating new subitem...")
                        
                        # Get all source subitem column values for initial sync
                        source_subitem_full = context.monday_api.get_subitem_with_all_columns(subitem_id)
                        
                        # Build column values dict for the new subitem
                        # We'll sync all syncable columns from the source
                        initial_column_values = {}
                        
                        if source_subitem_full:
                            source_columns_data = source_subitem_full.get('column_values', [])
                            
                            # We'll need target board columns - get from an existing subitem or create without values first
                            # Create the subitem first, then update columns
                            new_subitem_id = context.monday_api.create_subitem(
                                parent_item_id=linked_parent_id,
                                subitem_name=source_subitem_name
                            )
                            
                            if new_subitem_id:
                                logger.info(f"Created new subitem '{source_subitem_name}' (ID: {new_subitem_id}) in linked parent {linked_parent_id}")
                                
                                # Get the new subitem's board ID
                                new_subitem_info = context.monday_api.get_subitem_with_all_columns(new_subitem_id)
                                if new_subitem_info:
                                    target_subitem_board_id = new_subitem_info.get('board', {}).get('id')
                                    target_subitem_columns = context.monday_api.get_board_columns(target_subitem_board_id) if target_subitem_board_id else []
                                    
                                    # Sync all syncable columns from source to target
                                    columns_synced = 0
                                    for source_col in source_columns_data:
                                        source_col_id = source_col.get('id')
                                        source_col_value = source_col.get('value')
                                        
                                        if not source_col_value or source_col_value == 'null':
                                            continue
                                        
                                        # Find matching column in target
                                        matching_col = find_matching_column(source_subitem_columns, target_subitem_columns, source_col_id)
                                        
                                        if matching_col:
                                            try:
                                                success = context.monday_api.update_column_value(
                                                    target_subitem_board_id,
                                                    new_subitem_id,
                                                    matching_col['id'],
                                                    source_col_value
                                                )
                                                if success:
                                                    _mark_update_made(new_subitem_id, matching_col['id'])
                                                    columns_synced += 1
                                            except Exception as col_error:
                                                logger.warning(f"Failed to sync column {source_col_id}: {str(col_error)}")
                                    
                                    sync_results.append({
                                        'linked_parent_id': linked_parent_id,
                                        'target_subitem_id': new_subitem_id,
                                        'target_subitem_name': source_subitem_name,
                                        'target_board_id': target_subitem_board_id,
                                        'action': 'created',
                                        'columns_synced': columns_synced,
                                        'success': True
                                    })
                                    
                                    logger.info(f"Created and synced {columns_synced} columns for new subitem {new_subitem_id}")
                            else:
                                logger.error(f"Failed to create subitem '{source_subitem_name}' in linked parent {linked_parent_id}")
                                sync_results.append({
                                    'linked_parent_id': linked_parent_id,
                                    'error': 'Failed to create subitem',
                                    'success': False
                                })
                        
                        continue  # Move to next linked parent
                    
                    # Subitem exists - update the changed column
                    target_subitem_id = target_subitem.get('id')
                    target_subitem_board_id = target_subitem.get('board', {}).get('id')
                    
                    target_subitem_columns = context.monday_api.get_board_columns(target_subitem_board_id) if target_subitem_board_id else []
                    
                    # Find matching column
                    matching_column = find_matching_column(source_subitem_columns, target_subitem_columns, column_id)
                    
                    if not matching_column:
                        logger.info(f"No matching column for {column_id} in target subitem board")
                        continue
                    
                    # Get the value to sync
                    if isinstance(new_value, dict):
                        value_to_sync = json.dumps(new_value)
                    elif isinstance(new_value, str):
                        try:
                            json.loads(new_value)
                            value_to_sync = new_value
                        except:
                            col_value = get_column_value_for_sync(
                                subitem.get('column_values', []),
                                column_id
                            )
                            if col_value:
                                value_to_sync = col_value.get('value', '{}')
                            else:
                                value_to_sync = json.dumps({})
                    else:
                        value_to_sync = json.dumps({})
                    
                    # Update target subitem's column
                    success = context.monday_api.update_column_value(
                        target_subitem_board_id,
                        target_subitem_id,
                        matching_column['id'],
                        value_to_sync
                    )
                    
                    if success:
                        _mark_update_made(target_subitem_id, matching_column['id'])
                    
                    sync_results.append({
                        'linked_parent_id': linked_parent_id,
                        'target_subitem_id': target_subitem_id,
                        'target_subitem_name': target_subitem.get('name', ''),
                        'target_board_id': target_subitem_board_id,
                        'target_column_id': matching_column['id'],
                        'target_column_title': matching_column['title'],
                        'success': success
                    })
                    
                    logger.info(f"Synced subitem column {column_id} -> {matching_column['id']} for subitem {target_subitem_id}: {success}")
                    
                except Exception as e:
                    logger.error(f"Error syncing subitem to linked parent {linked_parent_id}: {str(e)}")
                    sync_results.append({
                        'linked_parent_id': linked_parent_id,
                        'error': str(e),
                        'success': False
                    })
        
        # Build response
        successful_syncs = sum(1 for r in sync_results if r.get('success'))
        
        return jsonify({
            'status': 'success',
            'message': f'Synced {successful_syncs} of {len(sync_results)} linked subitems',
            'source': {
                'subitem_id': subitem_id,
                'subitem_name': subitem.get('name', ''),
                'parent_item_id': parent_id,
                'parent_item_name': parent_item.get('name', ''),
                'parent_board_type': parent_board_type,
                'column_id': column_id
            },
            'sync_results': sync_results
        }), 200
        
    except Exception as e:
        logger.error(f"Error in sync-subitem endpoint: {str(e)}", exc_info=True)
        return jsonify({
            'error': str(e),
            'status': 'error'
        }), 500

# ==================== EPIC COLUMN SYNC ENDPOINT ====================

@bp.route('/api/sync-epic-columns', methods=['POST'])
def sync_epic_columns():
    """
    When the Epic (board_relation) column changes on a User Stories item, fetch the
    'Customer' and 'Product' text values from the linked Epic item and write them into
    the editable 'Customers' and 'Products' columns on the User Stories item.

    This lets users work without an Epic (filling the manual columns themselves) while
    still getting those fields auto-populated whenever an Epic IS selected.

    Expected POST body (Monday.com column_change webhook):
    {
        "event": {
            "type": "column_change",
            "pulseId":  "<user-stories-item-id>",
            "boardId":  "<user-stories-board-id>",
            "columnId": "<epic-board-relation-column-id>",
            "value": { "linkedPulseIds": [{ "linkedPulseId": <epic-item-id> }] }
        }
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided', 'status': 'error'}), 400

    if 'challenge' in data:
        return jsonify({'challenge': data['challenge']}), 200

    if not context.monday_api:
        return jsonify({'error': 'Monday.com API not configured', 'status': 'error'}), 500

    try:
        # Support nested JSON string format sent by some Monday.com automations
        if 'data' in data and isinstance(data['data'], str):
            try:
                data = json.loads(data['data'])
            except json.JSONDecodeError:
                pass

        event = data.get('event', data)

        item_id  = str(event.get('pulseId')  or event.get('itemId')  or event.get('item_id',  ''))
        board_id = str(event.get('boardId')  or event.get('board_id', ''))
        column_id = str(event.get('columnId') or event.get('column_id', ''))
        new_value = event.get('value', {})

        if not item_id or not board_id:
            return jsonify({'error': 'Missing required fields: pulseId/boardId', 'status': 'error'}), 400

        logger.info(f"sync-epic-columns: item={item_id}, board={board_id}, column={column_id}")

        # ── 1. Extract linked Epic item ID(s) from the new value ──────────────
        # Monday.com sends: { "linkedPulseIds": [{ "linkedPulseId": 12345 }] }
        # It may also arrive as a JSON string inside 'value'.
        if isinstance(new_value, str):
            try:
                new_value = json.loads(new_value)
            except json.JSONDecodeError:
                new_value = {}

        epic_item_ids = []
        if isinstance(new_value, dict):
            for entry in new_value.get('linkedPulseIds', []):
                lid = entry.get('linkedPulseId')
                if lid:
                    epic_item_ids.append(str(lid))

        if not epic_item_ids:
            logger.info(f"No Epic linked to item {item_id} — leaving manual columns unchanged")
            return jsonify({
                'status': 'skipped',
                'message': 'No Epic linked; manual Customers/Products columns were not changed'
            }), 200

        # Use the first linked Epic (items typically link to one Epic)
        epic_item_id = epic_item_ids[0]
        logger.info(f"Linked Epic item: {epic_item_id}")

        # ── 2. Fetch Epic item columns ─────────────────────────────────────────
        epic_item = context.monday_api.get_item_with_columns(epic_item_id)
        if not epic_item:
            return jsonify({'error': f'Epic item {epic_item_id} not found', 'status': 'error'}), 404

        epic_board_id = epic_item.get('board', {}).get('id')
        epic_columns  = context.monday_api.get_board_columns(epic_board_id) if epic_board_id else []
        epic_col_by_id = {col['id']: col for col in epic_columns}

        # ── 3. Find Customer and Product values on the Epic item ──────────────
        CUSTOMER_TITLE_PATTERNS = ['customer', 'customers', 'client', 'clients']
        PRODUCT_TITLE_PATTERNS  = ['product',  'products',  'project', 'projects']

        customer_text = ''
        product_text  = ''

        for cv in epic_item.get('column_values', []):
            col_meta = epic_col_by_id.get(cv.get('id', ''), {})
            title_lower = col_meta.get('title', '').lower()
            col_type    = col_meta.get('type', '')

            # Skip read-only computed column types
            if col_type in ('mirror', 'lookup', 'formula', 'auto_number'):
                continue

            text_val = (cv.get('text') or '').strip()

            if not customer_text and any(p in title_lower for p in CUSTOMER_TITLE_PATTERNS):
                customer_text = text_val
                logger.info(f"Epic Customer value: '{customer_text}' (column: {col_meta.get('title')})")

            elif not product_text and any(p in title_lower for p in PRODUCT_TITLE_PATTERNS):
                product_text = text_val
                logger.info(f"Epic Product value: '{product_text}' (column: {col_meta.get('title')})")

        # ── 4. Find 'Customers' and 'Products' editable columns on US board ───
        us_columns = context.monday_api.get_board_columns(board_id)

        # Target column names as configured by the user.
        # Primary match is by exact title; fallback uses the same patterns.
        TARGET_CUSTOMER_NAMES = ['customers', 'customer']
        TARGET_PRODUCT_NAMES  = ['products',  'product']
        READONLY_TYPES        = ('mirror', 'lookup', 'formula', 'auto_number')

        target_customer_col = None
        target_product_col  = None

        # Prefer exact title matches (case-insensitive), skip read-only columns
        for col in us_columns:
            title_lower = col.get('title', '').lower()
            col_type    = col.get('type', '')

            if col_type in READONLY_TYPES:
                continue

            if not target_customer_col and title_lower in TARGET_CUSTOMER_NAMES:
                target_customer_col = col

            if not target_product_col and title_lower in TARGET_PRODUCT_NAMES:
                target_product_col = col

        # ── 5. Update the editable columns ────────────────────────────────────
        def _format_value(text: str, col_type: str) -> str:
            """Format a plain-text value for a given Monday.com column type."""
            if col_type == 'text':
                return json.dumps(text)
            elif col_type == 'long_text':
                return json.dumps({'text': text})
            elif col_type in ('status', 'color'):
                return json.dumps({'label': text})
            elif col_type == 'dropdown':
                return json.dumps({'labels': [text]})
            else:
                return json.dumps(text)

        results = []

        if target_customer_col and customer_text:
            col_id   = target_customer_col['id']
            col_type = target_customer_col.get('type', 'text')
            value    = _format_value(customer_text, col_type)
            success  = context.monday_api.update_column_value(board_id, item_id, col_id, value, col_type)
            if success:
                _mark_update_made(item_id, col_id)
            results.append({'column': target_customer_col.get('title'), 'value': customer_text, 'success': success})
            logger.info(f"Updated '{target_customer_col.get('title')}' -> '{customer_text}': {success}")
        elif not target_customer_col:
            logger.warning("No editable 'Customers' column found on User Stories board")
        elif not customer_text:
            logger.info("Epic item has no Customer value to copy")

        if target_product_col and product_text:
            col_id   = target_product_col['id']
            col_type = target_product_col.get('type', 'text')
            value    = _format_value(product_text, col_type)
            success  = context.monday_api.update_column_value(board_id, item_id, col_id, value, col_type)
            if success:
                _mark_update_made(item_id, col_id)
            results.append({'column': target_product_col.get('title'), 'value': product_text, 'success': success})
            logger.info(f"Updated '{target_product_col.get('title')}' -> '{product_text}': {success}")
        elif not target_product_col:
            logger.warning("No editable 'Products' column found on User Stories board")
        elif not product_text:
            logger.info("Epic item has no Product value to copy")

        successful = sum(1 for r in results if r.get('success'))
        return jsonify({
            'status': 'success',
            'message': f'Updated {successful} of {len(results)} columns from Epic',
            'epic_item_id': epic_item_id,
            'results': results
        }), 200

    except Exception as e:
        logger.error(f"Error in sync-epic-columns: {str(e)}", exc_info=True)
        return jsonify({'error': str(e), 'status': 'error'}), 500
