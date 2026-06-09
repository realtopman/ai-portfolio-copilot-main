import json
import logging
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request

from api import context
from utils.helper import (
    convert_report_to_html,
    generate_client_summary,
    generate_general_summary,
    generate_sprint_report_table,
    group_hours_by_type,
)


logger = logging.getLogger(__name__)
bp = Blueprint("reports", __name__)

# ==================== SPRINT REPORT ENDPOINTS ====================

@bp.route('/api/sprint-report', methods=['GET'])
def generate_sprint_report():
    workspace_id = request.args.get('workspace_id')
    if not workspace_id:
        return jsonify({
            'error': 'workspace_id parameter is required (1 or 2)',
            'status': 'error'
        }), 400
    
    # Get optional parameters
    use_ai = request.args.get('use_ai', 'true').lower() == 'true'
    group_name = request.args.get('group_name', 'Sprint Report')
    
    # Start report generation in a separate thread to avoid request timeout
    thread = threading.Thread(
        target=generate_sprint_report_thread, 
        args=(workspace_id, use_ai, group_name)
    )
    thread.daemon = True
    thread.start()
    return jsonify({
        'status': 'started',
        'message': 'Sprint report generation has started in the background.'
    }), 202

def generate_sprint_report_thread(workspace_id, use_ai=True, group_name='Sprint Report'):
    """
    Automatically generate sprint reports for a specific workspace.
    Runs in a background thread at the START of a sprint.
    
    This generates a report showing:
    - Previous sprint: Tasks by status (Done, In QA, In Development, In Design, In Review, Blocked, In Transfer, In Progress)
    - Current sprint: All tasks treated as "planned"
    
    Args:
        workspace_id: Workspace ID (required)
        use_ai: Use OpenAI for summary generation (default: true)
        group_name: Group name to create items in (default: "Sprint Report")
    
    Returns:
        Saves report data to JSON file and logs results
    """
    if not context.monday_api:
        logger.error('Monday.com API not configured')
        return
    
    try:
        
        # Check if AI is available and requested
        if use_ai and not context.report_generator:
            logger.warning("AI requested but not available. Using fallback reports.")
            use_ai = False
        
        logger.info(f"Starting sprint report generation for workspace {workspace_id} (AI: {use_ai})")
        
        # Step 1: Find the Sprints board in the specific workspace
        sprints_board = context.monday_api.get_sprints_board(workspace_id=workspace_id)
        if not sprints_board:
            logger.error(f'Sprints board not found in workspace {workspace_id}')
            return
        
        logger.info(f"Found Sprints board: {sprints_board['name']}")
        
        # Step 2: Get active sprint (current) and previous sprint
        active_sprint = context.monday_api.get_active_sprint(sprints_board['id'])
        
        if not active_sprint:
            logger.error('No active sprint found')
            return
        
        # Get previous sprint (the one that just ended)
        previous_sprint = context.monday_api.get_previous_sprint(sprints_board['id'], active_sprint['id'])
        
        logger.info(f"Active (Current) Sprint: {active_sprint['name']}")
        if previous_sprint:
            logger.info(f"Previous Sprint: {previous_sprint['name']}")
        else:
            logger.warning("No previous sprint found - this may be the first sprint")
        
        # Step 3: Get stories from Sprint Backlog board
        # Previous sprint: get tasks with specific statuses (Done, In QA, In Development, In Design, In Review, Blocked, In Transfer, In Progress)
        previous_sprint_by_status = {
            'done': [],
            'in_qa': [],
            'in_development': [],
            'in_design': [],
            'in_review': [],
            'blocked': [],
            'in_transfer': [],
            'in_progress': []
        }
        
        if previous_sprint:
            previous_sprint_stories = context.monday_api.get_stories_from_user_stories_board(previous_sprint['id'], workspace_id=workspace_id)
            for story in previous_sprint_stories:
                status = story.get('status', '').lower()
                if status in ['done', 'completed']:
                    previous_sprint_by_status['done'].append(story)
                elif status in ['in qa', 'in-qa', 'qa']:
                    previous_sprint_by_status['in_qa'].append(story)
                elif status in ['in development', 'in-development', 'in dev', 'working on it']:
                    previous_sprint_by_status['in_development'].append(story)
                elif status in ['in design', 'in-design', 'design']:
                    previous_sprint_by_status['in_design'].append(story)
                elif status in ['in review', 'in-review', 'review']:
                    previous_sprint_by_status['in_review'].append(story)
                elif status in ['blocked', 'stuck']:
                    previous_sprint_by_status['blocked'].append(story)
                elif status in ['in transfer', 'in-transfer', 'transfer']:
                    previous_sprint_by_status['in_transfer'].append(story)
                elif status in ['in progress', 'in-progress']:
                    previous_sprint_by_status['in_progress'].append(story)
        
        # Current sprint: get all stories (treat all as planned for the new sprint)
        planned_current_sprint = []
        current_sprint_stories = context.monday_api.get_stories_from_user_stories_board(active_sprint['id'], workspace_id=workspace_id)
        planned_current_sprint = list(current_sprint_stories)  # All tasks from current sprint are "planned"
        
        logger.info(f"Previous sprint - Done: {len(previous_sprint_by_status['done'])}, In QA: {len(previous_sprint_by_status['in_qa'])}, In Development: {len(previous_sprint_by_status['in_development'])}, In Design: {len(previous_sprint_by_status['in_design'])}, In Review: {len(previous_sprint_by_status['in_review'])}, Blocked: {len(previous_sprint_by_status['blocked'])}, In Transfer: {len(previous_sprint_by_status['in_transfer'])}, In Progress: {len(previous_sprint_by_status['in_progress'])}")
        logger.info(f"Found {len(planned_current_sprint)} stories planned for current sprint")
        
        # Step 4: Group stories by client, product, and status
        reports_by_client = {}
        
        # Add previous sprint stories organized by status
        for status_key, stories_list in previous_sprint_by_status.items():
            for story in stories_list:
                client = story.get('client', 'Unknown Client')
                product = story.get('product') or story.get('project') or 'General'
                
                if client not in reports_by_client:
                    reports_by_client[client] = {}
                
                if product not in reports_by_client[client]:
                    reports_by_client[client][product] = {
                        'done_previous_sprint': [],
                        'in_qa_previous_sprint': [],
                        'in_development_previous_sprint': [],
                        'in_design_previous_sprint': [],
                        'in_review_previous_sprint': [],
                        'blocked_previous_sprint': [],
                        'in_transfer_previous_sprint': [],
                        'in_progress_previous_sprint': [],
                        'planned_current_sprint': []
                    }
                
                # Add to appropriate status bucket
                if status_key == 'done':
                    reports_by_client[client][product]['done_previous_sprint'].append(story)
                elif status_key == 'in_qa':
                    reports_by_client[client][product]['in_qa_previous_sprint'].append(story)
                elif status_key == 'in_development':
                    reports_by_client[client][product]['in_development_previous_sprint'].append(story)
                elif status_key == 'in_design':
                    reports_by_client[client][product]['in_design_previous_sprint'].append(story)
                elif status_key == 'in_review':
                    reports_by_client[client][product]['in_review_previous_sprint'].append(story)
                elif status_key == 'blocked':
                    reports_by_client[client][product]['blocked_previous_sprint'].append(story)
                elif status_key == 'in_transfer':
                    reports_by_client[client][product]['in_transfer_previous_sprint'].append(story)
                elif status_key == 'in_progress':
                    reports_by_client[client][product]['in_progress_previous_sprint'].append(story)
        
        # Add current sprint stories (all treated as planned)
        for story in planned_current_sprint:
            client = story.get('client', 'Unknown Client')
            product = story.get('product') or story.get('project') or 'General'
            
            if client not in reports_by_client:
                reports_by_client[client] = {}
            
            if product not in reports_by_client[client]:
                reports_by_client[client][product] = {
                    'done_previous_sprint': [],
                    'in_qa_previous_sprint': [],
                    'in_development_previous_sprint': [],
                    'in_design_previous_sprint': [],
                    'in_review_previous_sprint': [],
                    'blocked_previous_sprint': [],
                    'in_transfer_previous_sprint': [],
                    'in_progress_previous_sprint': [],
                    'planned_current_sprint': []
                }
            
            # All current sprint tasks go to planned_current_sprint
            reports_by_client[client][product]['planned_current_sprint'].append(story)
        
        logger.info(f"Grouped into {len(reports_by_client)} clients")
        
        # Step 5: Get all client boards in the same workspace
        client_boards = context.monday_api.get_all_client_boards(workspace_id=workspace_id)
        board_lookup = {board['name']: board for board in client_boards}
        
        # Step 6: Create reports on each client board
        created_reports = []
        
        for client, products in reports_by_client.items():
            # Find matching board
            board = None
            for board_name in board_lookup.keys():
                if client.lower() in board_name.lower():
                    board = board_lookup[board_name]
                    break
            
            if not board:
                logger.warning(f"No board found for client: {client}")
                continue
            
            # Create one report per product
            for product, stories in products.items():
                # Organize previous sprint items by status
                done_previous_sprint = stories['done_previous_sprint']
                in_qa_previous_sprint = stories['in_qa_previous_sprint']
                in_development_previous_sprint = stories['in_development_previous_sprint']
                in_design_previous_sprint = stories['in_design_previous_sprint']
                in_review_previous_sprint = stories['in_review_previous_sprint']
                blocked_previous_sprint = stories['blocked_previous_sprint']
                in_transfer_previous_sprint = stories['in_transfer_previous_sprint']
                in_progress_previous_sprint = stories['in_progress_previous_sprint']
                planned_current_sprint = stories['planned_current_sprint']
                
                # For report generation, combine in-progress items from previous sprint
                in_progress = in_qa_previous_sprint + in_development_previous_sprint + in_design_previous_sprint + in_review_previous_sprint + in_progress_previous_sprint
                blocked = blocked_previous_sprint
                
                # Generate hours by type table for this product
                all_previous_sprint = done_previous_sprint + in_qa_previous_sprint + in_development_previous_sprint + in_design_previous_sprint + in_review_previous_sprint + blocked_previous_sprint + in_transfer_previous_sprint + in_progress_previous_sprint
                
                est_prev_by_type = group_hours_by_type(all_previous_sprint, 'Estimated Effort')
                act_prev_by_type = group_hours_by_type(all_previous_sprint, 'Actual Effort')
                est_curr_by_type = group_hours_by_type(planned_current_sprint, 'Estimated Effort')
                
                # Create hours by type table rows
                all_types = set(est_prev_by_type.keys()) | set(act_prev_by_type.keys()) | set(est_curr_by_type.keys())
                hours_by_type_table = []
                
                logger.info(f"  Types for {client} - {product}: {all_types}")
                logger.info(f"  Hours - Est prev: {est_prev_by_type}, Act prev: {act_prev_by_type}, Est curr: {est_curr_by_type}")
                
                if all_types:
                    # Add header: Type, Time Spent only (no estimated column for client reports)
                    header_row = {
                        "cells": [
                            {"insert": "Type"},
                            {"insert": "Time Spent (h)"}
                        ]
                    }
                    hours_by_type_table.append(header_row)
                    
                    # Add rows for each type (sorted)
                    total_act_prev = 0.0
                    
                    for type_name in sorted(all_types):
                        act_prev = act_prev_by_type.get(type_name, 0.0)
                        
                        total_act_prev += act_prev
                        
                        type_row = {
                            "cells": [
                                {"insert": type_name},
                                {"insert": f"{act_prev:.1f}h" if act_prev > 0 else "-"}
                            ]
                        }
                        hours_by_type_table.append(type_row)
                    
                    # Add totals row
                    totals_row = {
                        "cells": [
                            {"insert": "Total"},
                            {"insert": f"{total_act_prev:.1f}h"}
                        ]
                    }
                    hours_by_type_table.append(totals_row)
                
                # Generate summary with simplified categorization
                try:
                    summary = context.report_generator.generate_sprint_report_simple(
                        done_previous_sprint,
                        in_transfer_previous_sprint,
                        in_progress,
                        blocked,
                        planned_current_sprint,
                        f"{client} - {product}",
                        hours_by_type_table=hours_by_type_table if hours_by_type_table else None
                    )
                except Exception as e:
                    logger.warning(f"AI generation failed: {str(e)}. Using fallback.")
                        
                # Get or create group
                group_id = context.monday_api.get_group_id_by_name(board['id'], group_name)
                if not group_id:
                    # Use first group if Weekly Report doesn't exist
                    group_id = board['groups'][0]['id'] if board.get('groups') else 'topics'
                
                # Find or create item for this product on the client board
                item_name = product
                item = context.monday_api.get_item_by_name(board['id'], item_name, group_id)
                
                if item:
                    # Item exists, use it
                    item_id = item['id']
                    logger.info(f"Found existing item '{item_name}' (ID: {item_id}) on board {board['name']}")
                else:
                    # Create new item for this product
                    item_id = context.monday_api.create_item_on_board(
                        board_id=board['id'],
                        group_id=group_id,
                        item_name=item_name,
                        column_values={}
                    )
                    logger.info(f"Created new item '{item_name}' (ID: {item_id}) on board {board['name']}")
                
                if item_id:
                    # Post the sprint report as an update to the item
                    # Convert plain text report to HTML format
                    summary_html = convert_report_to_html(summary)
                    # Use previous sprint name for the title (the sprint we're reporting on)
                    report_sprint_name = previous_sprint['name'] if previous_sprint else active_sprint['name']
                    update_title = f"📋 Sprint Report - {report_sprint_name} - {datetime.now().strftime('%Y-%m-%d')}"
                    update_body = f"<b>{update_title}</b><br><br>{summary_html}"
                    
                    success = context.monday_api.create_update_on_item(item_id, update_body)
                    
                    if success:
                        created_reports.append({
                            'client': client,
                            'product': product,
                            'board_name': board['name'],
                            'item_id': item_id,
                            'item_name': item_name,
                            'done_previous': len(done_previous_sprint),
                            'in_qa_previous': len(in_qa_previous_sprint),
                            'in_development_previous': len(in_development_previous_sprint),
                            'in_design_previous': len(in_design_previous_sprint),
                            'in_review_previous': len(in_review_previous_sprint),
                            'blocked_previous': len(blocked_previous_sprint),
                            'in_transfer_previous': len(in_transfer_previous_sprint),
                            'in_progress_previous': len(in_progress_previous_sprint),
                            'planned_current_sprint': len(planned_current_sprint),
                            'update_posted': True
                        })
                        
                        logger.info(f"Posted sprint report update for {client} - {product} on board {board['name']}")
                    else:
                        logger.error(f"Failed to post update for {client} - {product}")
        
        # Generate general summary for all projects
        general_summary = generate_general_summary(reports_by_client, use_ai, context.report_generator)
        
        # Generate table data (returns two tables)
        task_table_rows, hours_table_rows = generate_sprint_report_table(reports_by_client)
        
        # Extract sprint number from active sprint name (e.g., "Sprint 14" -> 14)
        sprint_number = None
        try:
            import re
            match = re.search(r'(?:Sprint\s*)?(\d+)', active_sprint['name'], re.IGNORECASE)
            if match:
                sprint_number = int(match.group(1))
                logger.info(f"Extracted current sprint number: {sprint_number}")
        except Exception as e:
            logger.warning(f"Could not extract sprint number from '{active_sprint['name']}': {str(e)}")
        
        # Update hours table header to include current sprint number
        if sprint_number and len(hours_table_rows) > 0:
            # Update the header row (first row) - replace "[CURRENT]" with actual number
            header_row = hours_table_rows[0]
            for cell in header_row.get('cells', []):
                if '[CURRENT]' in cell.get('insert', ''):
                    cell['insert'] = cell['insert'].replace('[CURRENT]', str(sprint_number))

        #Save task and hours tables as json file for logging
        with open(f"sprint_report_task_table_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", "w") as f:
            json.dump(task_table_rows, f, indent=4)
        with open(f"sprint_report_hours_table_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", "w") as f:
            json.dump(hours_table_rows, f, indent=4)
        
        # Create doc directly in Sprint Reports folder
        doc_id = None
        try:
            # Find Sprint Reports folder
            folder_id = context.monday_api.get_folder_id_by_name(workspace_id, "Sprint Reports")
            
            if folder_id:
                logger.info(f"Found Sprint Reports folder: {folder_id}")
                
                # Combine tables for doc creation
                combined_tables = [
                    {"title": "Task Details", "rows": task_table_rows},
                    {"title": "Hours Summary", "rows": hours_table_rows}
                ]
                
                # Create doc in folder with combined tables
                # Use previous sprint name for the title (the sprint we're reporting on)
                if previous_sprint:
                    # Extract sprint number from previous sprint name
                    prev_sprint_number = None
                    try:
                        prev_match = re.search(r'(?:Sprint\s*)?(\d+)', previous_sprint['name'], re.IGNORECASE)
                        if prev_match:
                            prev_sprint_number = int(prev_match.group(1))
                    except Exception:
                        pass
                    
                    if prev_sprint_number:
                        doc_title = f"Sprint Report {prev_sprint_number} - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                    else:
                        doc_title = f"Sprint Report - {previous_sprint['name']} - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                else:
                    # Fallback to active sprint if no previous sprint
                    if sprint_number:
                        doc_title = f"Sprint Report {sprint_number} - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                    else:
                        doc_title = f"Sprint Report - {active_sprint['name']} - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                doc_id = context.monday_api.create_doc_in_folder_with_table(workspace_id, folder_id, doc_title, general_summary, combined_tables)
                
                if doc_id:
                    logger.info(f"Created doc {doc_id} in Sprint Reports folder")
                else:
                    logger.error("Failed to create doc in Sprint Reports folder")
            else:
                logger.warning("Sprint Reports folder not found in workspace")
                
        except Exception as e:
            logger.error(f"Error creating doc in folder: {str(e)}")
        
        # Prepare complete report data
        report_data = {
            'status': 'success',
            'timestamp': datetime.now().isoformat(),
            'workspace_id': workspace_id,
            'message': f'Created {len(created_reports)} sprint reports (start of sprint)',
            'current_sprint': active_sprint['name'],
            'current_sprint_id': active_sprint['id'],
            'previous_sprint': previous_sprint['name'] if previous_sprint else None,
            'previous_sprint_id': previous_sprint['id'] if previous_sprint else None,
            'reports': created_reports,
            'summary': {
                'total_clients': len(reports_by_client),
                'total_reports': len(created_reports),
                'previous_sprint_stats': {
                    'total_done': sum(len(p['done_previous_sprint']) for products in reports_by_client.values() for p in products.values()),
                    'total_in_qa': sum(len(p['in_qa_previous_sprint']) for products in reports_by_client.values() for p in products.values()),
                    'total_in_development': sum(len(p['in_development_previous_sprint']) for products in reports_by_client.values() for p in products.values()),
                    'total_in_design': sum(len(p['in_design_previous_sprint']) for products in reports_by_client.values() for p in products.values()),
                    'total_in_review': sum(len(p['in_review_previous_sprint']) for products in reports_by_client.values() for p in products.values()),
                    'total_blocked': sum(len(p['blocked_previous_sprint']) for products in reports_by_client.values() for p in products.values()),
                    'total_in_transfer': sum(len(p['in_transfer_previous_sprint']) for products in reports_by_client.values() for p in products.values()),
                    'total_in_progress': sum(len(p['in_progress_previous_sprint']) for products in reports_by_client.values() for p in products.values()),
                },
                'current_sprint_stats': {
                    'total_planned': sum(len(p['planned_current_sprint']) for products in reports_by_client.values() for p in products.values()),
                }
            }
        }
        
        # Save to JSON file
        filename = f"sprint_report_created_workspace_{workspace_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Saved report data to {filename}")
        logger.info(f"Sprint report generation completed successfully for workspace {workspace_id}")
    
    except Exception as e:
        logger.error(f"Error generating reports for workspace {workspace_id}: {str(e)}", exc_info=True)


# ==================== LAST N SPRINTS REPORT ====================

@bp.route('/api/last-sprints-report', methods=['GET'])
def generate_last_sprints_report():
    """
    Temporary endpoint: generate sprint reports for the last N completed sprints.
    Defaults to the last 3 sprints.

    Query params:
        workspace_id  (required)
        num_sprints   (optional, default 3)
        use_ai        (optional, default true)
        group_name    (optional, default 'Sprint Report')
    """
    workspace_id = request.args.get('workspace_id')
    if not workspace_id:
        return jsonify({
            'error': 'workspace_id parameter is required',
            'status': 'error'
        }), 400

    try:
        num_sprints = int(request.args.get('num_sprints', 3))
    except ValueError:
        num_sprints = 3

    use_ai = request.args.get('use_ai', 'true').lower() == 'true'
    group_name = request.args.get('group_name', 'Sprint Report')

    thread = threading.Thread(
        target=generate_last_sprints_report_thread,
        args=(workspace_id, num_sprints, use_ai, group_name)
    )
    thread.daemon = True
    thread.start()

    return jsonify({
        'status': 'started',
        'message': f'Report generation for last {num_sprints} sprints has started in the background.',
        'workspace_id': workspace_id,
        'num_sprints': num_sprints
    }), 202


def generate_last_sprints_report_thread(workspace_id, num_sprints=3, use_ai=True, group_name='Sprint Report'):
    """
    Background thread: generate sprint reports for the last N completed sprints.

    For each sprint it:
      - Retrieves stories categorised by status
      - Posts updates on the matching client board items
      - Creates a doc in the 'Sprint Reports' folder
    """
    if not context.monday_api:
        logger.error('Monday.com API not configured')
        return

    try:
        if use_ai and not context.report_generator:
            logger.warning("AI requested but not available. Using fallback reports.")
            use_ai = False

        logger.info(f"[LastSprints] Starting report generation for workspace {workspace_id}, last {num_sprints} sprints")

        # ── 1. Find Sprints board ──────────────────────────────────────────────
        sprints_board = context.monday_api.get_sprints_board(workspace_id=workspace_id)
        if not sprints_board:
            logger.error(f'[LastSprints] Sprints board not found in workspace {workspace_id}')
            return

        # ── 2. Get active sprint so we know the baseline ──────────────────────
        active_sprint = context.monday_api.get_active_sprint(sprints_board['id'])
        if not active_sprint:
            logger.error('[LastSprints] No active sprint found')
            return

        logger.info(f"[LastSprints] Active sprint: {active_sprint['name']}")

        # ── 3. Get last N completed sprints ───────────────────────────────────
        previous_sprints = context.monday_api.get_previous_n_sprints(
            sprints_board['id'], active_sprint['id'], n=num_sprints
        )

        if not previous_sprints:
            logger.warning('[LastSprints] No previous sprints found')
            return

        logger.info(f"[LastSprints] Sprints to report on: {[s['name'] for s in previous_sprints]}")

        # ── 4. Get client boards (done once, reused for all sprints) ──────────
        client_boards = context.monday_api.get_all_client_boards(workspace_id=workspace_id)
        board_lookup = {board['name']: board for board in client_boards}

        # ── 5. Find Sprint Reports folder (once) ──────────────────────────────
        folder_id = None
        try:
            folder_id = context.monday_api.get_folder_id_by_name(workspace_id, "Sprint Reports")
            if folder_id:
                logger.info(f"[LastSprints] Found Sprint Reports folder: {folder_id}")
            else:
                logger.warning("[LastSprints] Sprint Reports folder not found in workspace")
        except Exception as e:
            logger.error(f"[LastSprints] Error finding Sprint Reports folder: {str(e)}")

        import re

        all_report_results = []

        # ── 6. Process each sprint ────────────────────────────────────────────
        for sprint in previous_sprints:
            sprint_name = sprint['name']
            sprint_id = sprint['id']
            logger.info(f"[LastSprints] ── Processing sprint: {sprint_name} ──")

            # 6a. Fetch & categorise stories for this sprint
            sprint_by_status = {
                'done': [], 'in_qa': [], 'in_development': [],
                'in_design': [], 'in_review': [], 'blocked': [],
                'in_transfer': [], 'in_progress': []
            }

            stories = context.monday_api.get_stories_from_user_stories_board(sprint_id, workspace_id=workspace_id)
            for story in stories:
                status = story.get('status', '').lower()
                if status in ['done', 'completed']:
                    sprint_by_status['done'].append(story)
                elif status in ['in qa', 'in-qa', 'qa']:
                    sprint_by_status['in_qa'].append(story)
                elif status in ['in development', 'in-development', 'in dev', 'working on it']:
                    sprint_by_status['in_development'].append(story)
                elif status in ['in design', 'in-design', 'design']:
                    sprint_by_status['in_design'].append(story)
                elif status in ['in review', 'in-review', 'review']:
                    sprint_by_status['in_review'].append(story)
                elif status in ['blocked', 'stuck']:
                    sprint_by_status['blocked'].append(story)
                elif status in ['in transfer', 'in-transfer', 'transfer']:
                    sprint_by_status['in_transfer'].append(story)
                elif status in ['in progress', 'in-progress']:
                    sprint_by_status['in_progress'].append(story)

            logger.info(
                f"[LastSprints] {sprint_name} – Done: {len(sprint_by_status['done'])}, "
                f"In QA: {len(sprint_by_status['in_qa'])}, In Dev: {len(sprint_by_status['in_development'])}, "
                f"In Design: {len(sprint_by_status['in_design'])}, In Review: {len(sprint_by_status['in_review'])}, "
                f"Blocked: {len(sprint_by_status['blocked'])}, In Transfer: {len(sprint_by_status['in_transfer'])}, "
                f"In Progress: {len(sprint_by_status['in_progress'])}"
            )

            # 6b. Group by client / product
            reports_by_client = {}
            status_keys = list(sprint_by_status.keys())

            for status_key in status_keys:
                for story in sprint_by_status[status_key]:
                    client = story.get('customers', 'Unknown Client')
                    product = story.get('products') or story.get('project') or 'General'
                    reports_by_client.setdefault(client, {})
                    if product not in reports_by_client[client]:
                        reports_by_client[client][product] = {k + '_sprint': [] for k in status_keys}
                    reports_by_client[client][product][status_key + '_sprint'].append(story)

            logger.info(f"[LastSprints] {sprint_name} – grouped into {len(reports_by_client)} client(s)")

            # Extract sprint number for headers
            sprint_number = None
            try:
                m = re.search(r'(?:Sprint\s*)?(\d+)', sprint_name, re.IGNORECASE)
                if m:
                    sprint_number = int(m.group(1))
            except Exception:
                pass

            created_reports = []

            # 6c. Post updates on client boards
            for client, products in reports_by_client.items():
                board = None
                for board_name in board_lookup:
                    if client.lower() in board_name.lower():
                        board = board_lookup[board_name]
                        break

                if not board:
                    logger.warning(f"[LastSprints] No board found for client: {client}")
                    continue

                for product, story_buckets in products.items():
                    done_stories = story_buckets.get('done_sprint', [])
                    in_qa_stories = story_buckets.get('in_qa_sprint', [])
                    in_dev_stories = story_buckets.get('in_development_sprint', [])
                    in_design_stories = story_buckets.get('in_design_sprint', [])
                    in_review_stories = story_buckets.get('in_review_sprint', [])
                    blocked_stories = story_buckets.get('blocked_sprint', [])
                    in_transfer_stories = story_buckets.get('in_transfer_sprint', [])
                    in_progress_stories = story_buckets.get('in_progress_sprint', [])

                    all_stories = (
                        done_stories + in_qa_stories + in_dev_stories +
                        in_design_stories + in_review_stories + blocked_stories +
                        in_transfer_stories + in_progress_stories
                    )

                    # Build hours-by-type table (2 columns: Time Spent, this sprint estimated)
                    act_by_type = group_hours_by_type(all_stories, 'Actual Effort')
                    est_by_type = group_hours_by_type(all_stories, 'Estimated Effort')
                    all_types = set(act_by_type.keys()) | set(est_by_type.keys())

                    hours_by_type_table = []
                    if all_types:
                        hours_by_type_table.append({
                            "cells": [
                                {"insert": "Type"},
                                {"insert": "Time Spent (h)"},
                                {"insert": f"Sprint {sprint_number} estimated (h)" if sprint_number else f"{sprint_name} estimated (h)"}
                            ]
                        })
                        total_act = 0.0
                        total_est = 0.0
                        for type_name in sorted(all_types):
                            act = act_by_type.get(type_name, 0.0)
                            est = est_by_type.get(type_name, 0.0)
                            total_act += act
                            total_est += est
                            hours_by_type_table.append({
                                "cells": [
                                    {"insert": type_name},
                                    {"insert": f"{act:.1f}h" if act > 0 else "-"},
                                    {"insert": f"{est:.1f}h" if est > 0 else "-"}
                                ]
                            })
                        hours_by_type_table.append({
                            "cells": [
                                {"insert": "Total"},
                                {"insert": f"{total_act:.1f}h"},
                                {"insert": f"{total_est:.1f}h"}
                            ]
                        })

                    # Generate summary text
                    in_progress_combined = (
                        in_qa_stories + in_dev_stories + in_design_stories +
                        in_review_stories + in_progress_stories
                    )
                    try:
                        if use_ai and context.report_generator:
                            summary = context.report_generator.generate_sprint_report_simple(
                                done_stories,
                                in_transfer_stories,
                                in_progress_combined,
                                blocked_stories,
                                [],  # no "next sprint planned" for historical sprints
                                f"{client} - {product}",
                                hours_by_type_table=hours_by_type_table if hours_by_type_table else None
                            )
                        else:
                            summary = generate_client_summary(
                                client, product,
                                done_stories, in_qa_stories, in_dev_stories,
                                in_design_stories, in_review_stories, blocked_stories,
                                in_transfer_stories, in_progress_stories,
                                []
                            )
                    except Exception as e:
                        logger.warning(f"[LastSprints] Summary generation failed for {client}/{product}: {str(e)}")
                        summary = f"Sprint report for {sprint_name} – {client} / {product}"

                    # Find/create item on the client board
                    group_id = context.monday_api.get_group_id_by_name(board['id'], group_name)
                    if not group_id:
                        group_id = board['groups'][0]['id'] if board.get('groups') else 'topics'

                    item = context.monday_api.get_item_by_name(board['id'], product, group_id)
                    if item:
                        item_id = item['id']
                    else:
                        item_id = context.monday_api.create_item_on_board(
                            board_id=board['id'],
                            group_id=group_id,
                            item_name=product,
                            column_values={}
                        )

                    if item_id:
                        summary_html = convert_report_to_html(summary)
                        update_title = f"📋 Sprint Report - {sprint_name} - {datetime.now().strftime('%Y-%m-%d')}"
                        update_body = f"<b>{update_title}</b><br><br>{summary_html}"
                        success = context.monday_api.create_update_on_item(item_id, update_body)

                        if success:
                            created_reports.append({
                                'sprint': sprint_name,
                                'client': client,
                                'product': product,
                                'board_name': board['name'],
                                'item_id': item_id,
                            })
                            logger.info(f"[LastSprints] Posted report for {sprint_name} – {client}/{product}")
                        else:
                            logger.error(f"[LastSprints] Failed to post update for {client}/{product}")

            # 6d. Create doc in Sprint Reports folder for this sprint
            if folder_id:
                try:
                    general_summary = generate_general_summary(reports_by_client, use_ai, context.report_generator)
                    task_table_rows, hours_table_rows = generate_sprint_report_table(reports_by_client)

                    # Fix "[CURRENT]" placeholder if present
                    if sprint_number and hours_table_rows:
                        for cell in hours_table_rows[0].get('cells', []):
                            if '[CURRENT]' in cell.get('insert', ''):
                                cell['insert'] = cell['insert'].replace('[CURRENT]', str(sprint_number))

                    combined_tables = [
                        {"title": "Task Details", "rows": task_table_rows},
                        {"title": "Hours Summary", "rows": hours_table_rows}
                    ]

                    if sprint_number:
                        doc_title = f"Sprint Report {sprint_number} - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                    else:
                        doc_title = f"Sprint Report - {sprint_name} - {datetime.now().strftime('%Y-%m-%d %H:%M')}"

                    doc_id = context.monday_api.create_doc_in_folder_with_table(
                        workspace_id, folder_id, doc_title, general_summary, combined_tables
                    )
                    if doc_id:
                        logger.info(f"[LastSprints] Created doc '{doc_title}' (ID: {doc_id})")
                    else:
                        logger.error(f"[LastSprints] Failed to create doc for sprint {sprint_name}")
                except Exception as e:
                    logger.error(f"[LastSprints] Error creating doc for sprint {sprint_name}: {str(e)}")

            all_report_results.append({
                'sprint': sprint_name,
                'reports_created': len(created_reports),
                'reports': created_reports
            })

        # ── 7. Save summary JSON ──────────────────────────────────────────────
        summary_data = {
            'status': 'success',
            'timestamp': datetime.now().isoformat(),
            'workspace_id': workspace_id,
            'num_sprints_requested': num_sprints,
            'sprints_processed': [s['name'] for s in previous_sprints],
            'results': all_report_results
        }
        filename = f"last_sprints_report_workspace_{workspace_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(summary_data, f, indent=2, ensure_ascii=False)

        logger.info(f"[LastSprints] Completed. Summary saved to {filename}")

    except Exception as e:
        logger.error(f"[LastSprints] Error for workspace {workspace_id}: {str(e)}", exc_info=True)
