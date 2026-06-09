from flask import Flask, render_template, request, jsonify
import os
import logging
import json
from datetime import datetime
from dotenv import load_dotenv
import threading
import time
import timebuzzer as timebuzzer_sync
# Load environment variables from .env file FIRST (before importing config)
load_dotenv()

# Loop prevention: Cache of recent updates we've made (to prevent endless webhook loops)
# Key: (item_id, column_id) -> Value: timestamp when we last updated it
_recent_updates_cache = {}
_recent_updates_lock = threading.Lock()
RECENT_UPDATE_TTL_SECONDS = 5  # Skip webhooks for updates we made within this window

def _mark_update_made(item_id: str, column_id: str):
    """Mark that we just made an update, to prevent processing the resulting webhook."""
    key = (str(item_id), str(column_id))
    with _recent_updates_lock:
        _recent_updates_cache[key] = time.time()
        # Cleanup old entries
        now = time.time()
        expired_keys = [k for k, v in _recent_updates_cache.items() if now - v > RECENT_UPDATE_TTL_SECONDS * 2]
        for k in expired_keys:
            del _recent_updates_cache[k]

def _is_self_triggered_update(item_id: str, column_id: str) -> bool:
    """Check if this update was recently made by us (to prevent loop)."""
    key = (str(item_id), str(column_id))
    with _recent_updates_lock:
        if key in _recent_updates_cache:
            if time.time() - _recent_updates_cache[key] < RECENT_UPDATE_TTL_SECONDS:
                return True
            # Expired, remove it
            del _recent_updates_cache[key]
    return False

from config import config
from monday_api import MondayAPI
from openai_helper import ReportGenerator
from helper import (
    get_hours_from_story,
    convert_report_to_html,
    generate_table_as_text,
    generate_sprint_report_table,
    generate_hours_by_type_table,
    group_hours_by_type,
    generate_general_summary,
    generate_client_summary,
    # Column sync helpers
    identify_board_type,
    get_link_column_id,
    find_matching_column,
    extract_linked_item_ids,
    get_column_value_for_sync,
    format_column_value_for_update,
    find_matching_subitem,
    build_sync_response,
    SYNCABLE_COLUMNS,
    # Move to Sprints helpers
    is_move_to_sprints_status,
    is_ir_sprint_status,
    extract_status_label_from_value,
    CROSS_BOARD_COLUMN_MAPPING,
    # In Transfer helpers
    is_in_transfer_status,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
env = os.environ.get('FLASK_ENV', 'development')
app.config.from_object(config[env])

# Initialize integrations
monday_api = None
report_generator = None

def init_integrations():
    """Initialize external API integrations."""
    global monday_api, report_generator
    
    monday_token = app.config.get('MONDAY_API_TOKEN')
    openai_key = app.config.get('OPENAI_API_KEY')
    logger.info(f"Initializing integrations...")
    logger.info(f"MONDAY_API_TOKEN present: {bool(monday_token)}")
    logger.info(f"OPENAI_API_KEY present: {bool(openai_key)}")
    
    if monday_token:
        # Get workspace IDs from config - using both workspaces
        workspace_ids = []
        workspace_1 = app.config.get('WORKSPACE_ID_1')
        
        if workspace_1:
            workspace_ids.append(workspace_1)
        
        monday_api = MondayAPI(
            api_token=monday_token,
            api_url=app.config['MONDAY_API_URL'],
            workspace_ids=workspace_ids
        )
        logger.info(f"Monday API initialized successfully with workspace filter: {workspace_ids}")
    else:
        logger.error("MONDAY_API_TOKEN not found in config!")
    
    if openai_key:
        try:
            report_generator = ReportGenerator(
                api_key=openai_key,
                model=app.config.get('OPENAI_MODEL', 'gpt-3.5-turbo')
            )
            logger.info("OpenAI API initialized successfully")
        except Exception as e:
            logger.warning(f"OpenAI initialization failed: {str(e)}. Will use fallback reports.")
            report_generator = None
    else:
        logger.info("OpenAI API key not provided. Will use fallback reports.")

# Initialize integrations on startup


# ==================== EXAMPLE ROUTES ====================

@app.route('/')
def index():
    """Home endpoint."""
    return jsonify({
        'message': 'Sprint Report API Server',
        'status': 'active',
        'version': '1.0.0'
    })

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint."""

    print("""Performing health check...""")
    integrations_status = {
        'monday_api' : monday_api is not None,
        'openai': report_generator is not None
    }
    return jsonify({
        'status': 'healthy',
        'integrations': integrations_status
    })

# ==================== SPRINT REPORT ENDPOINTS ====================

@app.route('/api/sprint-report', methods=['GET'])
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
    if not monday_api:
        logger.error('Monday.com API not configured')
        return
    
    try:
        
        # Check if AI is available and requested
        if use_ai and not report_generator:
            logger.warning("AI requested but not available. Using fallback reports.")
            use_ai = False
        
        logger.info(f"Starting sprint report generation for workspace {workspace_id} (AI: {use_ai})")
        
        # Step 1: Find the Sprints board in the specific workspace
        sprints_board = monday_api.get_sprints_board(workspace_id=workspace_id)
        if not sprints_board:
            logger.error(f'Sprints board not found in workspace {workspace_id}')
            return
        
        logger.info(f"Found Sprints board: {sprints_board['name']}")
        
        # Step 2: Get active sprint (current) and previous sprint
        active_sprint = monday_api.get_active_sprint(sprints_board['id'])
        
        if not active_sprint:
            logger.error('No active sprint found')
            return
        
        # Get previous sprint (the one that just ended)
        previous_sprint = monday_api.get_previous_sprint(sprints_board['id'], active_sprint['id'])
        
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
            previous_sprint_stories = monday_api.get_stories_from_user_stories_board(previous_sprint['id'], workspace_id=workspace_id)
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
        current_sprint_stories = monday_api.get_stories_from_user_stories_board(active_sprint['id'], workspace_id=workspace_id)
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
        client_boards = monday_api.get_all_client_boards(workspace_id=workspace_id)
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
                    summary = report_generator.generate_sprint_report_simple(
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
                group_id = monday_api.get_group_id_by_name(board['id'], group_name)
                if not group_id:
                    # Use first group if Weekly Report doesn't exist
                    group_id = board['groups'][0]['id'] if board.get('groups') else 'topics'
                
                # Find or create item for this product on the client board
                item_name = product
                item = monday_api.get_item_by_name(board['id'], item_name, group_id)
                
                if item:
                    # Item exists, use it
                    item_id = item['id']
                    logger.info(f"Found existing item '{item_name}' (ID: {item_id}) on board {board['name']}")
                else:
                    # Create new item for this product
                    item_id = monday_api.create_item_on_board(
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
                    
                    success = monday_api.create_update_on_item(item_id, update_body)
                    
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
        general_summary = generate_general_summary(reports_by_client, use_ai, report_generator)
        
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
            folder_id = monday_api.get_folder_id_by_name(workspace_id, "Sprint Reports")
            
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
                doc_id = monday_api.create_doc_in_folder_with_table(workspace_id, folder_id, doc_title, general_summary, combined_tables)
                
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

@app.route('/api/last-sprints-report', methods=['GET'])
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
    if not monday_api:
        logger.error('Monday.com API not configured')
        return

    try:
        if use_ai and not report_generator:
            logger.warning("AI requested but not available. Using fallback reports.")
            use_ai = False

        logger.info(f"[LastSprints] Starting report generation for workspace {workspace_id}, last {num_sprints} sprints")

        # ── 1. Find Sprints board ──────────────────────────────────────────────
        sprints_board = monday_api.get_sprints_board(workspace_id=workspace_id)
        if not sprints_board:
            logger.error(f'[LastSprints] Sprints board not found in workspace {workspace_id}')
            return

        # ── 2. Get active sprint so we know the baseline ──────────────────────
        active_sprint = monday_api.get_active_sprint(sprints_board['id'])
        if not active_sprint:
            logger.error('[LastSprints] No active sprint found')
            return

        logger.info(f"[LastSprints] Active sprint: {active_sprint['name']}")

        # ── 3. Get last N completed sprints ───────────────────────────────────
        previous_sprints = monday_api.get_previous_n_sprints(
            sprints_board['id'], active_sprint['id'], n=num_sprints
        )

        if not previous_sprints:
            logger.warning('[LastSprints] No previous sprints found')
            return

        logger.info(f"[LastSprints] Sprints to report on: {[s['name'] for s in previous_sprints]}")

        # ── 4. Get client boards (done once, reused for all sprints) ──────────
        client_boards = monday_api.get_all_client_boards(workspace_id=workspace_id)
        board_lookup = {board['name']: board for board in client_boards}

        # ── 5. Find Sprint Reports folder (once) ──────────────────────────────
        folder_id = None
        try:
            folder_id = monday_api.get_folder_id_by_name(workspace_id, "Sprint Reports")
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

            stories = monday_api.get_stories_from_user_stories_board(sprint_id, workspace_id=workspace_id)
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
                        if use_ai and report_generator:
                            summary = report_generator.generate_sprint_report_simple(
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
                    group_id = monday_api.get_group_id_by_name(board['id'], group_name)
                    if not group_id:
                        group_id = board['groups'][0]['id'] if board.get('groups') else 'topics'

                    item = monday_api.get_item_by_name(board['id'], product, group_id)
                    if item:
                        item_id = item['id']
                    else:
                        item_id = monday_api.create_item_on_board(
                            board_id=board['id'],
                            group_id=group_id,
                            item_name=product,
                            column_values={}
                        )

                    if item_id:
                        summary_html = convert_report_to_html(summary)
                        update_title = f"📋 Sprint Report - {sprint_name} - {datetime.now().strftime('%Y-%m-%d')}"
                        update_body = f"<b>{update_title}</b><br><br>{summary_html}"
                        success = monday_api.create_update_on_item(item_id, update_body)

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
                    general_summary = generate_general_summary(reports_by_client, use_ai, report_generator)
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

                    doc_id = monday_api.create_doc_in_folder_with_table(
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


# ==================== HOURS CALCULATION HELPERS ====================

# Column titles that represent estimated and actual hours
HOURS_COLUMN_TITLES = {
    'estimated': ['est. hrs', 'est hrs', 'estimated hrs', 'estimated hours', 'estimation', 'estimate'],
    'actual': ['actual hrs', 'actual hours', 'actual', 'actuals']
}

def is_hours_column(column_title: str) -> str:
    """
    Check if a column title represents an hours column.
    Returns 'estimated', 'actual', or None.
    """
    if not column_title:
        return None
    title_lower = column_title.lower().strip().rstrip('.')
    for hours_type, titles in HOURS_COLUMN_TITLES.items():
        if title_lower in titles or any(t in title_lower for t in titles):
            return hours_type
    return None


def calculate_hours_from_subitems(subitems: list, column_title: str) -> float:
    """
    Calculate the sum of hours from subitems for a given column.
    
    Args:
        subitems: List of subitem dicts with column_values
        column_title: Title of the hours column (e.g., 'Est. Hrs', 'Actual Hrs')
        
    Returns:
        Sum of hours values from all subitems
    """
    total = 0.0
    title_lower = column_title.lower().strip().rstrip('.')
    hours_type = is_hours_column(column_title)
    
    for subitem in subitems:
        for cv in subitem.get('column_values', []):
            # Match by similar column title - try both formats
            cv_title = cv.get('title', '') or cv.get('column', {}).get('title', '') or ''
            cv_title_lower = cv_title.lower().strip().rstrip('.')
            cv_hours_type = is_hours_column(cv_title)
            
            # Check if titles match (handle variations like "Est. Hrs" vs "Estimated Hrs")
            if cv_title_lower == title_lower or (cv_hours_type == hours_type and hours_type):
                # Extract numeric value
                value = cv.get('value')
                text = cv.get('text', '')
                
                if value and value != 'null':
                    try:
                        parsed = json.loads(value) if isinstance(value, str) else value
                        if isinstance(parsed, dict) and 'value' in parsed:
                            total += float(parsed['value'] or 0)
                        elif isinstance(parsed, (int, float)):
                            total += float(parsed)
                        elif isinstance(parsed, str):
                            total += float(parsed.replace(',', ''))
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
                elif text:
                    try:
                        # Try to parse from text (might be "5" or "5.5")
                        total += float(text.replace(',', '').replace(' Hrs', '').replace('Hrs', '').strip())
                    except ValueError:
                        pass
                break  # Found matching column, move to next subitem
    
    return total


def find_hours_column_id(columns: list, hours_type: str) -> tuple:
    """
    Find the column ID for estimated or actual hours.
    
    Args:
        columns: List of column dicts
        hours_type: 'estimated' or 'actual'
        
    Returns:
        Tuple of (column_id, column_title) or (None, None)
    """
    search_titles = HOURS_COLUMN_TITLES.get(hours_type, [])
    
    for col in columns:
        col_title = col.get('title', '').lower().strip().rstrip('.')
        col_type = col.get('type', '')
        
        # Only match numeric columns
        if col_type not in ['numeric', 'numbers']:
            continue
            
        if col_title in search_titles or any(t in col_title for t in search_titles):
            return col['id'], col.get('title', '')
    
    return None, None


# ==================== COLUMN SYNC ENDPOINTS ====================

@app.route('/api/sync-item', methods=['POST'])
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
    if not monday_api:
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
        source_board = monday_api.get_board_by_id(board_id)
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
        source_item = monday_api.get_item_with_columns(item_id)
        if not source_item:
            return jsonify({'error': f'Item {item_id} not found', 'status': 'error'}), 404
        
        # Get source board columns
        source_columns = monday_api.get_board_columns(board_id)
        
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
                                linked_item = monday_api.get_item_with_columns(linked_item_id)
                                if not linked_item:
                                    logger.warning(f"Linked item {linked_item_id} not found")
                                    continue
                                
                                linked_board_id = linked_item.get('board', {}).get('id')
                                if not linked_board_id:
                                    continue
                                
                                # Get target board columns
                                target_columns = monday_api.get_board_columns(linked_board_id)
                                
                                # Find matching status column
                                matching_column = find_matching_column(source_columns, target_columns, column_id)
                                
                                if matching_column:
                                    # Update the status on the linked item
                                    value_to_sync = json.dumps(new_value) if isinstance(new_value, dict) else new_value
                                    success = monday_api.update_column_value(
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
                    user_stories_board = monday_api.get_user_stories_board(workspace_id)
                    if not user_stories_board:
                        return jsonify({
                            'error': 'Sprint Backlog board not found',
                            'status': 'error'
                        }), 404
                    
                    # Get active sprint
                    active_sprint = monday_api.get_active_sprint_for_user_stories(workspace_id)
                    if not active_sprint:
                        logger.warning("No active sprint found - item will be created without sprint assignment")
                    
                    # Get User Stories board columns for mapping
                    us_columns = monday_api.get_board_columns(user_stories_board['id'])
                    
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
                    new_item_id = monday_api.create_item_with_columns(
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
                    source_subitems = monday_api.get_item_subitems(item_id)
                    created_subitems = []
                    
                    if source_subitems:
                        logger.info(f"Copying {len(source_subitems)} subitems to new item")
                        
                        for subitem in source_subitems:
                            subitem_name = subitem.get('name', 'Untitled Subitem')
                            
                            # Get source subitem board columns
                            source_subitem_board_id = subitem.get('board', {}).get('id')
                            source_subitem_columns = monday_api.get_board_columns(source_subitem_board_id) if source_subitem_board_id else []
                            
                            # Create the subitem first (we'll update columns after)
                            new_subitem_id = monday_api.create_subitem(
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
                                new_subitem_info = monday_api.get_subitem_with_all_columns(new_subitem_id)
                                if new_subitem_info:
                                    target_subitem_board_id = new_subitem_info.get('board', {}).get('id')
                                    target_subitem_columns = monday_api.get_board_columns(target_subitem_board_id) if target_subitem_board_id else []
                                    
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
                                                    monday_api.update_column_value(
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
                                        monday_api.update_column_value(
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
                            monday_api.update_column_value(
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
                subitems = monday_api.get_item_subitems(item_id)
                
                if subitems and len(subitems) > 0:
                    logger.info(f"Item has {len(subitems)} subitems - recalculating {hours_type} hours from sum")
                    
                    # Calculate sum from subitems
                    calculated_sum = calculate_hours_from_subitems(subitems, column_title)
                    logger.info(f"Calculated {hours_type} hours from subitems: {calculated_sum}")
                    
                    # Update the source item with the calculated sum
                    success = monday_api.update_column_value(
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
                    linked_item = monday_api.get_item_with_columns(linked_item_id)
                    if not linked_item:
                        logger.warning(f"Linked item {linked_item_id} not found")
                        continue
                    
                    linked_board_id = linked_item.get('board', {}).get('id')
                    if not linked_board_id:
                        logger.warning(f"Could not get board ID for linked item {linked_item_id}")
                        continue
                    
                    # Get target board columns
                    target_columns = monday_api.get_board_columns(linked_board_id)
                    
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
                    success = monday_api.update_column_value(
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


@app.route('/api/sync-subitem', methods=['POST'])
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
    if not monday_api:
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
        subitem = monday_api.get_subitem_with_parent(subitem_id)
        if not subitem:
            return jsonify({'error': f'Subitem {subitem_id} not found', 'status': 'error'}), 404
        
        # If parent_item not in subitem response, use the parentItemId from event
        if not subitem.get('parent_item') and event_parent_item_id:
            logger.info(f"Using parentItemId from event: {event_parent_item_id}")
            parent_item_from_event = monday_api.get_item_with_columns(event_parent_item_id)
            if parent_item_from_event:
                subitem['parent_item'] = parent_item_from_event
                # Get board name if we only have board ID
                if event_parent_board_id and not parent_item_from_event.get('board', {}).get('name'):
                    parent_board_info = monday_api.get_board_by_id(event_parent_board_id)
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
        parent_columns = monday_api.get_board_columns(parent_board_id)
        
        # Get subitem board columns to identify the changed column
        subitem_board_id = subitem.get('board', {}).get('id') or board_id
        subitem_columns = monday_api.get_board_columns(subitem_board_id) if subitem_board_id else []
        
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
                all_subitems = monday_api.get_item_subitems(parent_id)
                
                if all_subitems:
                    # Calculate sum from all subitems
                    calculated_sum = calculate_hours_from_subitems(all_subitems, column_title)
                    logger.info(f"Calculated parent's {hours_type} hours from {len(all_subitems)} subitems: {calculated_sum}")
                    
                    # Find the corresponding column in parent board
                    parent_hours_col_id, parent_hours_col_title = find_hours_column_id(parent_columns, hours_type)
                    
                    if parent_hours_col_id:
                        # Update parent item with calculated sum
                        success = monday_api.update_column_value(
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
                        linked_parent = monday_api.get_item_with_columns(linked_parent_id)
                        if not linked_parent:
                            continue
                        
                        linked_parent_board_id = linked_parent.get('board', {}).get('id')
                        if not linked_parent_board_id:
                            continue
                        
                        linked_parent_columns = monday_api.get_board_columns(linked_parent_board_id)
                        
                        for parent_hours_col_id, parent_hours_col_title, hours_value in parent_hours_sync_needed:
                            # Determine hours type
                            hours_type = is_hours_column(parent_hours_col_title)
                            if not hours_type:
                                continue
                            
                            # Find matching hours column in linked parent board
                            target_hours_col_id, target_hours_col_title = find_hours_column_id(linked_parent_columns, hours_type)
                            
                            if target_hours_col_id:
                                success = monday_api.update_column_value(
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
                    linked_subitems = monday_api.get_item_subitems(linked_parent_id)
                    
                    # Find matching subitem by name
                    source_subitem_name = subitem.get('name', '')
                    target_subitem = find_matching_subitem(source_subitem_name, linked_subitems) if linked_subitems else None
                    
                    # Get source subitem board info for column mapping
                    source_subitem_board_id = subitem.get('board', {}).get('id')
                    source_subitem_columns = monday_api.get_board_columns(source_subitem_board_id) if source_subitem_board_id else []
                    
                    # If no matching subitem found, create it
                    if not target_subitem:
                        logger.info(f"No matching subitem found for '{source_subitem_name}' in linked parent {linked_parent_id}. Creating new subitem...")
                        
                        # Get all source subitem column values for initial sync
                        source_subitem_full = monday_api.get_subitem_with_all_columns(subitem_id)
                        
                        # Build column values dict for the new subitem
                        # We'll sync all syncable columns from the source
                        initial_column_values = {}
                        
                        if source_subitem_full:
                            source_columns_data = source_subitem_full.get('column_values', [])
                            
                            # We'll need target board columns - get from an existing subitem or create without values first
                            # Create the subitem first, then update columns
                            new_subitem_id = monday_api.create_subitem(
                                parent_item_id=linked_parent_id,
                                subitem_name=source_subitem_name
                            )
                            
                            if new_subitem_id:
                                logger.info(f"Created new subitem '{source_subitem_name}' (ID: {new_subitem_id}) in linked parent {linked_parent_id}")
                                
                                # Get the new subitem's board ID
                                new_subitem_info = monday_api.get_subitem_with_all_columns(new_subitem_id)
                                if new_subitem_info:
                                    target_subitem_board_id = new_subitem_info.get('board', {}).get('id')
                                    target_subitem_columns = monday_api.get_board_columns(target_subitem_board_id) if target_subitem_board_id else []
                                    
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
                                                success = monday_api.update_column_value(
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
                    
                    target_subitem_columns = monday_api.get_board_columns(target_subitem_board_id) if target_subitem_board_id else []
                    
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
                    success = monday_api.update_column_value(
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

@app.route('/api/sync-epic-columns', methods=['POST'])
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

    if not monday_api:
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
        epic_item = monday_api.get_item_with_columns(epic_item_id)
        if not epic_item:
            return jsonify({'error': f'Epic item {epic_item_id} not found', 'status': 'error'}), 404

        epic_board_id = epic_item.get('board', {}).get('id')
        epic_columns  = monday_api.get_board_columns(epic_board_id) if epic_board_id else []
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
        us_columns = monday_api.get_board_columns(board_id)

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
            success  = monday_api.update_column_value(board_id, item_id, col_id, value, col_type)
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
            success  = monday_api.update_column_value(board_id, item_id, col_id, value, col_type)
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
TIMEBUZZER_ACTIVITY_CACHE_PATH = os.environ.get(
    "TIMEBUZZER_ACTIVITY_CACHE_FILE",
    os.path.join(os.path.dirname(__file__), ".timebuzzer_activity_cache.json"),
)
TIMEBUZZER_ACTIVITY_CACHE_LOCK = threading.Lock()


class TimeBuzzerActivityDetailsUnavailable(Exception):
    pass


def load_timebuzzer_activity_cache():
    if TIMEBUZZER_ACTIVITY_CACHE:
        return
    try:
        with open(TIMEBUZZER_ACTIVITY_CACHE_PATH, "r", encoding="utf-8") as cache_file:
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
        with open(TIMEBUZZER_ACTIVITY_CACHE_PATH, "w", encoding="utf-8") as cache_file:
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
    api_key = app.config.get("TIMEBUZZER_API_KEY") or os.environ.get("TIMEBUZZER_API_KEY")
    if not api_key:
        raise RuntimeError("TIMEBUZZER_API_KEY is missing.")
    return timebuzzer_sync.TimeBuzzerClient(
        api_key,
        base_url=app.config.get("TIMEBUZZER_BASE_URL", timebuzzer_sync.TIMEBUZZER_BASE_URL),
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
    if monday_api is None:
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
        target = monday_api.get_subitem_with_parent(target_id)
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
        target = monday_api.get_item_with_columns(target_id)
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

    columns = monday_api.get_board_columns(board_id)
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
    updated = monday_api.update_column_value(board_id, target_id, actual_col_id, value, "numeric")
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


@app.route("/api/timebuzzer/newActivity", methods=["POST"])
def receive_timebuzzer_new_activity():
    return handle_timebuzzer_activity("new")


@app.route("/api/timebuzzer/editActivity", methods=["POST"])
def receive_timebuzzer_eidt_activity():
    return handle_timebuzzer_activity("edit")


receive_timebuzzer_edit_activity = receive_timebuzzer_eidt_activity


@app.route("/api/timebuzzer/deleteActivity", methods=["POST"])
def receive_timebuzzer_delete_activity():
    return handle_timebuzzer_activity("delete")


@app.route("/api/timebuzzer/sync", methods=["GET", "POST"])
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
        request_value(payload, "workspace_id", app.config.get("WORKSPACE_ID_2") or timebuzzer_sync.DEFAULT_WORKSPACE_ID)
    )
    latest_sprints = parse_int(request_value(payload, "latest_sprints"), 2)
    execute = parse_bool(request_value(payload, "execute"), True)
    progress = parse_bool(request_value(payload, "progress"), True)
    layer_ids_arg = request_value(payload, "layer_ids")
    base_url = request_value(payload, "base_url", timebuzzer_sync.TIMEBUZZER_BASE_URL)

    if latest_sprints < 1:
        return jsonify({
            "status": "error",
            "error": "latest_sprints must be 1 or greater",
        }), 400

    layer_ids_error = validate_layer_ids_csv(layer_ids_arg or os.environ.get("TIMEBUZZER_LAYER_IDS"))
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


# ==================== IN TRANSFER ENDPOINT ====================

@app.route('/api/transfer-to-sprint', methods=['POST'])
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

    if not monday_api:
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
        source_board = monday_api.get_board_by_id(board_id)
        workspace_id = str(source_board.get('workspace', {}).get('id', '')) if source_board else ''

        sprints_board = monday_api.get_sprints_board(workspace_id=workspace_id if workspace_id else None)
        if not sprints_board:
            return jsonify({'error': 'Sprints board not found', 'status': 'error'}), 404

        latest_sprint = monday_api.get_latest_sprint(sprints_board['id'])
        if not latest_sprint:
            return jsonify({'error': 'No latest sprint found', 'status': 'error'}), 404

        logger.info(f"[transfer-to-sprint] Latest sprint: {latest_sprint['name']} (ID: {latest_sprint['id']})")

        # ── Find the Sprint column and update it on the item ─────────────────
        board_columns = monday_api.get_board_columns(board_id)
        sprint_col_id = None
        for col in board_columns:
            if col.get('type') == 'board_relation' and 'sprint' in col.get('title', '').lower():
                sprint_col_id = col['id']
                break

        if not sprint_col_id:
            logger.warning(f"[transfer-to-sprint] No Sprint board_relation column found on board {board_id}")
            return jsonify({'error': 'Sprint column not found on board', 'status': 'error'}), 404

        sprint_value = json.dumps({"item_ids": [int(latest_sprint['id'])]})
        sprint_updated = monday_api.update_column_value(board_id, item_id, sprint_col_id, sprint_value)
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



# ==================== ERROR HANDLERS ====================

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not Found', 'status': 'error'}), 404

@app.errorhandler(500)
def server_error(error):
    logger.error(f"Server error: {str(error)}")
    return jsonify({'error': 'Internal Server Error', 'status': 'error'}), 500

# Initialize integrations when module is loaded (for both direct execution and pm2)
init_integrations()

if __name__ == '__main__':
    # use_reloader=False on Windows to avoid socket errors
    # Change files and restart manually, or use external tools like nodemon
    app.run(
        debug=True,
        host="0.0.0.0",
        port=5002,
        use_reloader=False,
        threaded=True
    )
