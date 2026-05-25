"""
Helper functions for sprint report generation.
"""
import re
import logging

logger = logging.getLogger(__name__)


def get_hours_from_story(story, field_name):
    """Extract hours from story - centralized helper."""
    try:
        if 'columns_by_name' in story and field_name in story['columns_by_name']:
            value = story['columns_by_name'][field_name].get('text', '0')
            if value and value != '':
                return float(value)
        
        if field_name == 'Estimated Effort':
            value = story.get('story_points', '0')
            if value and value != '':
                return float(value)
        elif field_name == 'Actual Effort':
            value = story.get('actual_effort', '0')
            if value and value != '':
                return float(value)
        
        return 0.0
    except (ValueError, TypeError):
        return 0.0


def group_hours_by_type(stories, effort_field):
    """
    Group hours from stories by their subitems' type field.
    
    Args:
        stories: List of story dictionaries (each with subitems)
        effort_field: Field name to extract ('Estimated Effort' or 'Actual Effort')
        
    Returns:
        Dictionary with structure: {type_name: hours_value}
        Items without a type are grouped as 'Untyped'
    """
    hours_by_type = {}
    
    for story in stories:
        subitems = story.get('subitems', [])
        
        if not subitems:
            # If story has no subitems, use parent story's hours under 'Untyped'
            parent_hours = get_hours_from_story(story, effort_field)
            if parent_hours > 0:
                hours_by_type['Untyped'] = hours_by_type.get('Untyped', 0) + parent_hours
        else:
            # Process each subitem
            for subitem in subitems:
                # Get type from subitem column_values
                subitem_type = None
                column_values = subitem.get('column_values', [])
                
                # Type column IDs for different workspaces
                type_column_ids = ['color_mkx03j4s', 'color_mkyej1tv']
                
                # Try both workspace type column IDs
                if isinstance(column_values, list):
                    for col_data in column_values:
                        if isinstance(col_data, dict) and col_data.get('id') in type_column_ids:
                            text_val = col_data.get('text', '')
                            if text_val:
                                subitem_type = text_val
                                break
                    if not subitem_type:
                        # If not found in list, break to avoid checking dict path
                        pass
                elif isinstance(column_values, dict):
                    # Handle case where column_values is a dict - try both column IDs
                    for col_id in type_column_ids:
                        col_data = column_values.get(col_id)
                        if col_data:
                            if isinstance(col_data, dict):
                                text_val = col_data.get('text', '')
                            else:
                                text_val = str(col_data)
                            if text_val:
                                subitem_type = text_val
                                break
                
                # Use 'Untyped' if no type found
                if not subitem_type:
                    subitem_type = 'Untyped'
                
                # Extract hours from subitem
                subitem_hours = 0.0
                if effort_field == 'Estimated Effort':
                    # Look for numeric column (estimated effort)
                    if isinstance(column_values, list):
                        for col_data in column_values:
                            if isinstance(col_data, dict) and col_data.get('id') == 'numeric' and col_data.get('type') == 'numbers':
                                try:
                                    val = col_data.get('text', '0')
                                    subitem_hours = float(val) if val else 0.0
                                    break
                                except (ValueError, TypeError):
                                    pass
                    elif isinstance(column_values, dict):
                        col_data = column_values.get('numeric')
                        if col_data:
                            try:
                                val = col_data.get('text', '0') if isinstance(col_data, dict) else str(col_data)
                                subitem_hours = float(val) if val else 0.0
                            except (ValueError, TypeError):
                                pass
                elif effort_field == 'Actual Effort':
                    # Look for numeric5 column (actual effort)
                    if isinstance(column_values, list):
                        for col_data in column_values:
                            if isinstance(col_data, dict) and col_data.get('id') == 'numeric5' and col_data.get('type') == 'numbers':
                                try:
                                    val = col_data.get('text', '0')
                                    subitem_hours = float(val) if val else 0.0
                                    break
                                except (ValueError, TypeError):
                                    pass
                    elif isinstance(column_values, dict):
                        col_data = column_values.get('numeric5')
                        if col_data:
                            try:
                                val = col_data.get('text', '0') if isinstance(col_data, dict) else str(col_data)
                                subitem_hours = float(val) if val else 0.0
                            except (ValueError, TypeError):
                                pass
                
                # Add to hours_by_type
                if subitem_hours > 0:
                    hours_by_type[subitem_type] = hours_by_type.get(subitem_type, 0) + subitem_hours
    
    return hours_by_type


def convert_report_to_html(report_text):
    """
    Convert plain text report to HTML format for Monday.com updates.
    
    Args:
        report_text: Plain text report with line breaks
        
    Returns:
        HTML formatted string
    """
    if not report_text:
        return ""
    
    # Convert line breaks to <br> tags
    html = report_text.replace('\n', '<br>')
    
    # Convert markdown-style bold to HTML bold
    html = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', html)
    
    return html


def generate_table_as_text(table_rows):
    """Convert table rows to formatted text representation."""
    if not table_rows:
        return ""
    
    # Calculate column widths based on content
    col_widths = []
    for col_idx in range(len(table_rows[0]["cells"])):
        max_width = max(len(str(row["cells"][col_idx]["insert"])) for row in table_rows)
        col_widths.append(max(max_width + 2, 12))  # Min width of 12
    
    # Build the text table
    table_text = "\n\n" + "=" * 150 + "\n"
    table_text += "📊 WEEKLY REPORT TABLE\n"
    table_text += "=" * 150 + "\n\n"
    
    for row_idx, row in enumerate(table_rows):
        # Add separator line before header and summary
        if row_idx == 0 or row_idx == len(table_rows) - 1:
            table_text += "-" * 150 + "\n"
        
        # Add row content
        row_parts = []
        for col_idx, cell in enumerate(row["cells"]):
            cell_text = str(cell["insert"])
            # For multi-line cells (tasks), show count + first task
            if '\n' in cell_text and row_idx > 0 and row_idx < len(table_rows) - 1:
                lines = cell_text.split('\n')
                if len(lines) > 1:
                    cell_text = f"({len(lines)}) {lines[0][:30]}..."
                else:
                    cell_text = lines[0][:35]
            row_parts.append(cell_text[:35].ljust(col_widths[col_idx]))
        
        table_text += " | ".join(row_parts) + "\n"
        
        # Add separator after header
        if row_idx == 0:
            table_text += "-" * 150 + "\n"
    
    table_text += "=" * 150 + "\n\n"
    
    return table_text


def generate_sprint_report_table(reports_by_client):
    """Generate two separate table data structures: one for tasks, one for hours.
    
    This is for start-of-sprint reports:
    - Previous sprint: Shows tasks by status (Done, In QA, In Development, etc.)
    - Current sprint: Shows all planned tasks
    """
    task_rows = []
    hours_rows = []
    
    # ==================== TASKS TABLE ====================
    task_header_row = {
        "cells": [
            {"insert": "Clients and Projects"},
            {"insert": "Completed"},
            {"insert": "In Progress"},
            {"insert": "Blocked"},
            {"insert": "Transferred"},
            {"insert": "Planned Next Sprint"}
        ]
    }
    task_rows.append(task_header_row)
    
    # ==================== HOURS TABLE ====================
    # Note: sprint_number should be passed as parameter, but for backwards compatibility
    # we'll use placeholder that will be updated in app.py
    hours_header_row = {
        "cells": [
            {"insert": "Clients and Projects"},
            {"insert": "Initial estimation"},
            {"insert": "Time Spent"},
            {"insert": "Sprint [CURRENT] estimated"}
        ]
    }
    hours_rows.append(hours_header_row)
    
    # Totals tracking
    total_completed = 0
    total_in_progress = 0
    total_blocked = 0
    total_transferred = 0
    total_planned = 0
    total_est_prev = 0.0
    total_act_prev = 0.0
    total_est_curr = 0.0
    
    # Data rows - iterate through clients and projects
    for client, products in sorted(reports_by_client.items()):
        for product, stories in sorted(products.items()):
            # Get task lists from previous sprint
            done = stories.get('done_previous_sprint', [])
            in_qa = stories.get('in_qa_previous_sprint', [])
            in_development = stories.get('in_development_previous_sprint', [])
            in_design = stories.get('in_design_previous_sprint', [])
            in_review = stories.get('in_review_previous_sprint', [])
            blocked = stories.get('blocked_previous_sprint', [])
            in_transfer = stories.get('in_transfer_previous_sprint', [])
            in_progress_status = stories.get('in_progress_previous_sprint', [])
            
            # Get planned tasks from current sprint
            planned = stories.get('planned_current_sprint', [])
            
            # Combine in-progress items from previous sprint
            in_progress = in_qa + in_development + in_design + in_review + in_progress_status
            
            # Calculate hours
            all_previous_sprint = done + in_qa + in_development + in_design + in_review + blocked + in_transfer + in_progress_status
            est_prev = sum(get_hours_from_story(s, 'Estimated Effort') for s in all_previous_sprint)
            act_prev = sum(get_hours_from_story(s, 'Actual Effort') for s in all_previous_sprint)
            est_curr = sum(get_hours_from_story(s, 'Estimated Effort') for s in planned)
            
            # Update totals
            total_completed += len(done)
            total_in_progress += len(in_progress)
            total_blocked += len(blocked)
            total_transferred += len(in_transfer)
            total_planned += len(planned)
            total_est_prev += est_prev
            total_act_prev += act_prev
            total_est_curr += est_curr
            
            # Format task names
            completed_tasks = '\n'.join([s['name'] for s in done]) if done else ''
            in_progress_tasks = '\n'.join([s['name'] for s in in_progress]) if in_progress else ''
            blocked_tasks = '\n'.join([s['name'] for s in blocked]) if blocked else ''
            transferred_tasks = '\n'.join([s['name'] for s in in_transfer]) if in_transfer else ''
            planned_tasks = '\n'.join([s['name'] for s in planned]) if planned else ''
            
            # Add TASKS row
            task_row = {
                "cells": [
                    {"insert": f"{client} - {product}"},
                    {"insert": completed_tasks},
                    {"insert": in_progress_tasks},
                    {"insert": blocked_tasks},
                    {"insert": transferred_tasks},
                    {"insert": planned_tasks}
                ]
            }
            task_rows.append(task_row)
            
            # Add HOURS row
            hours_row = {
                "cells": [
                    {"insert": f"{client} - {product}"},
                    {"insert": f"{est_prev:.1f}h" if est_prev > 0 else "-"},
                    {"insert": f"{act_prev:.1f}h" if act_prev > 0 else "-"},
                    {"insert": f"{est_curr:.1f}h" if est_curr > 0 else "-"}
                ]
            }
            hours_rows.append(hours_row)
    
    # Tasks Summary row
    task_summary_row = {
        "cells": [
            {"insert": "Total"},
            {"insert": f"{total_completed} tasks"},
            {"insert": f"{total_in_progress} tasks"},
            {"insert": f"{total_blocked} tasks"},
            {"insert": f"{total_transferred} tasks"},
            {"insert": f"{total_planned} tasks"}
        ]
    }
    task_rows.append(task_summary_row)
    
    # Hours Summary row
    hours_summary_row = {
        "cells": [
            {"insert": "Total"},
            {"insert": f"{total_est_prev:.1f}h"},
            {"insert": f"{total_act_prev:.1f}h"},
            {"insert": f"{total_est_curr:.1f}h"}
        ]
    }
    hours_rows.append(hours_summary_row)
    
    return task_rows, hours_rows


def generate_hours_by_type_table(reports_by_client):
    """
    Generate a table with hours grouped by type for each client-product combination.
    
    Returns:
        Dictionary with structure: {(client, product): hours_by_type_table_rows}
        Where each table has columns: Type, Previous Est, Previous Actual, Current Est
    """
    hours_by_type_tables = {}
    
    # Data rows - iterate through clients and projects
    for client, products in sorted(reports_by_client.items()):
        for product, stories in sorted(products.items()):
            # Get task lists from previous sprint
            done = stories.get('done_previous_sprint', [])
            in_qa = stories.get('in_qa_previous_sprint', [])
            in_development = stories.get('in_development_previous_sprint', [])
            in_design = stories.get('in_design_previous_sprint', [])
            in_review = stories.get('in_review_previous_sprint', [])
            blocked = stories.get('blocked_previous_sprint', [])
            in_transfer = stories.get('in_transfer_previous_sprint', [])
            in_progress_status = stories.get('in_progress_previous_sprint', [])
            
            # Get planned tasks from current sprint
            planned = stories.get('planned_current_sprint', [])
            
            # Combine all previous sprint items
            all_previous_sprint = done + in_qa + in_development + in_design + in_review + blocked + in_transfer + in_progress_status
            
            # Group hours by type
            est_prev_by_type = group_hours_by_type(all_previous_sprint, 'Estimated Effort')
            act_prev_by_type = group_hours_by_type(all_previous_sprint, 'Actual Effort')
            est_curr_by_type = group_hours_by_type(planned, 'Estimated Effort')
            
            # Collect all types across all categories
            all_types = set(est_prev_by_type.keys()) | set(act_prev_by_type.keys()) | set(est_curr_by_type.keys())
            
            # Only create a table if there are types with hours
            if all_types:
                table_rows = []
                
                # Add header
                header_row = {
                    "cells": [
                        {"insert": "Type"},
                        {"insert": "Initial estimation (h)"},
                        {"insert": "Time Spent (h)"},
                        {"insert": "Sprint [CURRENT] estimated (h)"}
                    ]
                }
                table_rows.append(header_row)
                
                # Add rows for each type (sorted)
                total_est_prev = 0.0
                total_act_prev = 0.0
                total_est_curr = 0.0
                
                for type_name in sorted(all_types):
                    est_prev = est_prev_by_type.get(type_name, 0.0)
                    act_prev = act_prev_by_type.get(type_name, 0.0)
                    est_curr = est_curr_by_type.get(type_name, 0.0)
                    
                    total_est_prev += est_prev
                    total_act_prev += act_prev
                    total_est_curr += est_curr
                    
                    type_row = {
                        "cells": [
                            {"insert": type_name},
                            {"insert": f"{est_prev:.1f}h" if est_prev > 0 else "-"},
                            {"insert": f"{act_prev:.1f}h" if act_prev > 0 else "-"},
                            {"insert": f"{est_curr:.1f}h" if est_curr > 0 else "-"}
                        ]
                    }
                    table_rows.append(type_row)
                
                # Add totals row
                totals_row = {
                    "cells": [
                        {"insert": "Total"},
                        {"insert": f"{total_est_prev:.1f}h"},
                        {"insert": f"{total_act_prev:.1f}h"},
                        {"insert": f"{total_est_curr:.1f}h"}
                    ]
                }
                table_rows.append(totals_row)
                
                hours_by_type_tables[(client, product)] = table_rows
    
    return hours_by_type_tables


def generate_general_summary(reports_by_client, use_ai, report_generator):
    """
    Generate consolidated summary with AI narrative followed by structured project breakdown.
    
    This is for start-of-sprint reports:
    - Previous sprint: Shows tasks by status (Done, In QA, In Development, etc.)
    - Current sprint: Shows all planned tasks
    """
    if not isinstance(reports_by_client, dict):
        logger.error(f"reports_by_client is not a dict, got {type(reports_by_client)}")
        return "Error: Invalid input to generate_general_summary"
    
    report = "📊 SPRINT START REPORT - ALL PROJECTS\n"
    report += "=" * 70 + "\n\n"
    
    all_projects = []
    try:
        for client, products in reports_by_client.items():
            if not isinstance(products, dict):
                logger.warning(f"Product for {client} is not dict: {type(products)}")
                continue
            
            for product, stories in products.items():
                if not isinstance(stories, dict):
                    logger.warning(f"Stories for {client}-{product} is not dict: {type(stories)}")
                    continue
                
                all_projects.append({
                    'client': client,
                    'product': product,
                    'stories': stories
                })
    except Exception as e:
        logger.error(f"STEP 2 FAILED: Error building all_projects: {str(e)}", exc_info=True)
        return f"Error: Failed to build project list: {str(e)}"
    
    # Prepare detailed project information for AI
    project_details = []
    try:
        for proj in sorted(all_projects, key=lambda x: (x['client'], x['product'])):
            client = proj['client']
            product = proj['product']
            stories = proj['stories']
            
            try:
                # Previous sprint statuses
                done = stories.get('done_previous_sprint', [])
                in_qa = stories.get('in_qa_previous_sprint', [])
                in_development = stories.get('in_development_previous_sprint', [])
                in_design = stories.get('in_design_previous_sprint', [])
                in_review = stories.get('in_review_previous_sprint', [])
                blocked = stories.get('blocked_previous_sprint', [])
                in_transfer = stories.get('in_transfer_previous_sprint', [])
                in_progress_status = stories.get('in_progress_previous_sprint', [])
                
                # Current sprint planned
                planned = stories.get('planned_current_sprint', [])
                
                # Combine in-progress from previous sprint
                in_progress = in_qa + in_development + in_design + in_review + in_progress_status
                
                # Helper function to extract update text
                def get_update_text(updates_list):
                    if not updates_list or not isinstance(updates_list, list):
                        return 'No updates'
                    try:
                        last_update = updates_list[-1] if updates_list else None
                        if not last_update:
                            return 'No updates'
                        if isinstance(last_update, dict):
                            return last_update.get('text') or last_update.get('body') or last_update.get('message') or 'No updates'
                        return str(last_update)[:150] if last_update else 'No updates'
                    except (KeyError, IndexError, TypeError):
                        return 'No updates'
                
                # Format completed tasks with details
                completed_details = []
                for s in done:
                    try:
                        updates = s.get('updates', []) if isinstance(s.get('updates'), list) else []
                        latest_update = get_update_text(updates)
                        completed_details.append({
                            'name': s.get('name', 'Unknown'),
                            'latest_update': latest_update
                        })
                    except Exception as e:
                        logger.warning(f"      Error processing done task: {str(e)}")
                        completed_details.append({'name': 'Error reading task', 'latest_update': 'N/A'})
                
                # Format in-progress tasks
                in_progress_details = []
                for s in in_progress:
                    try:
                        updates = s.get('updates', []) if isinstance(s.get('updates'), list) else []
                        latest_update = get_update_text(updates)
                        in_progress_details.append({
                            'name': s.get('name', 'Unknown'),
                            'latest_update': latest_update
                        })
                    except Exception as e:
                        logger.warning(f"      Error processing in-progress task: {str(e)}")
                        in_progress_details.append({'name': 'Error reading task', 'latest_update': 'N/A'})
                
                # Format blocked tasks
                blocked_details = []
                for s in blocked:
                    try:
                        updates = s.get('updates', []) if isinstance(s.get('updates'), list) else []
                        latest_update = get_update_text(updates)
                        blocked_details.append({
                            'name': s.get('name', 'Unknown'),
                            'latest_update': latest_update
                        })
                    except Exception as e:
                        logger.warning(f"      Error processing blocked task: {str(e)}")
                        blocked_details.append({'name': 'Error reading task', 'latest_update': 'N/A'})
                
                # Format planned tasks
                planned_details = []
                for s in planned:
                    try:
                        planned_details.append(s.get('name', 'Unknown'))
                    except Exception as e:
                        logger.warning(f"      Error processing planned task: {str(e)}")
                        planned_details.append('Error reading task')

                # Format transfer tasks
                transfer_details = []
                for s in in_transfer:
                    try:
                        transfer_details.append(s.get('name', 'Unknown'))
                    except Exception as e:
                        logger.warning(f"      Error processing transfer task: {str(e)}")
                        transfer_details.append('Error reading task')
                
                # Calculate hours
                all_prev = done + in_qa + in_development + in_design + in_review + blocked + in_transfer + in_progress_status
                est_prev = sum(get_hours_from_story(s, 'Estimated Effort') for s in all_prev)
                act_prev = sum(get_hours_from_story(s, 'Actual Effort') for s in all_prev)
                est_curr = sum(get_hours_from_story(s, 'Estimated Effort') for s in planned)
                
                project_details.append({
                    'client': client,
                    'product': product,
                    'completed': completed_details,
                    'in_progress': in_progress_details,
                    'blocked': blocked_details,
                    'in_transfer': transfer_details,
                    'planned': planned_details,
                    'hours': {
                        'est_previous': est_prev,
                        'act_previous': act_prev,
                        'est_current': est_curr
                    }
                })
                
            except Exception as e:
                logger.error(f"Error processing {client} - {product}: {str(e)}", exc_info=True)
                continue
    except Exception as e:
        logger.error(f"Error preparing project details: {str(e)}", exc_info=True)
        return f"Error: Failed to prepare project details: {str(e)}"
    # Use AI to generate narrative summary if available
    ai_narrative = ""
    if use_ai and report_generator:
        try:
            # Build overall totals for accuracy
            total_done = 0
            total_in_progress = 0
            total_blocked = 0
            total_in_transfer = 0
            total_planned = 0
            total_est_previous = 0.0
            total_act_previous = 0.0
            total_est_current = 0.0

            for proj in project_details:
                total_done += len(proj.get('completed', []))
                total_in_progress += len(proj.get('in_progress', []))
                total_blocked += len(proj.get('blocked', []))
                total_in_transfer += len(proj.get('in_transfer', []))
                total_planned += len(proj.get('planned', []))
                hours = proj.get('hours', {})
                total_est_previous += float(hours.get('est_previous', 0.0) or 0.0)
                total_act_previous += float(hours.get('act_previous', 0.0) or 0.0)
                total_est_current += float(hours.get('est_current', 0.0) or 0.0)

            # Format project data for AI
            projects_text = ""
            for proj in project_details:
                projects_text += f"\n\n**{proj['client']} - {proj['product']}**\n"
                
                if proj['completed']:
                    projects_text += f"Completed Previous Sprint ({len(proj['completed'])} items):\n"
                    for item in proj['completed']:
                        projects_text += f"  • {item['name']}\n"
                        if item['latest_update'] and item['latest_update'] != 'No updates':
                            projects_text += f"    Latest update: {item['latest_update'][:100]}\n"
                
                if proj['in_progress']:
                    projects_text += f"\nIn Progress Previous Sprint ({len(proj['in_progress'])} items):\n"
                    for item in proj['in_progress']:
                        projects_text += f"  • {item['name']}\n"
                        if item['latest_update'] and item['latest_update'] != 'No updates':
                            projects_text += f"    Latest update: {item['latest_update'][:100]}\n"
                
                if proj['blocked']:
                    projects_text += f"\nBlocked Previous Sprint ({len(proj['blocked'])} items):\n"
                    for item in proj['blocked']:
                        projects_text += f"  • {item['name']}\n"
                        if item['latest_update'] and item['latest_update'] != 'No updates':
                            projects_text += f"    Latest update: {item['latest_update'][:100]}\n"

                if proj['in_transfer']:
                    projects_text += f"\nTransferred from Previous Sprint ({len(proj['in_transfer'])} items):\n"
                    for item_name in proj['in_transfer']:
                        projects_text += f"  • {item_name}\n"
                
                if proj['planned']:
                    projects_text += f"\nPlanned Current Sprint ({len(proj['planned'])} items):\n"
                    for item_name in proj['planned']:
                        projects_text += f"  • {item_name}\n"
                
                projects_text += f"\nHours - Previous Sprint: Est. {proj['hours']['est_previous']:.1f}h, Actual {proj['hours']['act_previous']:.1f}h\n"
                projects_text += f"Hours - Current Sprint: Est. {proj['hours']['est_current']:.1f}h\n"
            
            prompt = f"""Generate a factual narrative summary for a sprint report.

This report is generated at the START of a new sprint. Analyze the completed and planned items below and write a summary of what was done and what is planned.

TOTALS:
- This sprint completed: {total_done} items, {total_in_progress} in progress, {total_blocked} blocked, {total_in_transfer} transferred
- This sprint hours: Estimated {total_est_previous:.1f}h, Actual {total_act_previous:.1f}h
- Upcoming sprint: {total_planned} items planned, Estimated {total_est_current:.1f}h

PROJECT DETAILS AND ITEM NAMES:
{projects_text}

WRITING INSTRUCTIONS:
1. First paragraph: Summarize what was COMPLETED this sprint. Analyze the item names and group related work together. Mention the main areas of work (e.g., "Customer Management features", "Login and Authentication", "Dashboard development"). Include the total hours logged.

2. Second paragraph: Summarize what is PLANNED for the upcoming sprint. Analyze the planned item names and describe the main focus areas. Include the estimated hours.

3. Third paragraph (if applicable): Mention any items in transfer, in progress, or blocked that need attention.

STYLE REQUIREMENTS:
- Be factual and direct
- Group similar items together instead of listing each one
- Mention specific features/modules by name when they represent major work
- Include numbers (items count, hours)
- No adjectives like "successfully", "excellent", "great", "impressive"
- No marketing language
- Keep it to 3 paragraphs, around 8-12 sentences total

EXAMPLE OUTPUT:
"This sprint, the team completed 12 items totaling 263.5 hours. The main work included Customer Management (list view, invite flow, edit/delete), User Login and Password Reset functionality, Dashboard and Home screen development, and Services/Rooms module integration.

For the upcoming sprint, 25 items are planned with an estimated 291.5 hours. The focus will be on Event Management updates, Chat functionality implementation, 3D Tour integration, Navigation refactoring, and Admin Dashboard development.

2 items remain in transfer from this sprint: infrastructure setup and Hi-Fi prototypes.\"
"""
            
            ai_response = report_generator.client.chat.completions.create(
                model=report_generator.model,
                messages=[
                    {"role": "system", "content": "You are a factual report writer. Analyze the item names to identify main work areas and summarize them clearly. Write in a direct, objective style. Group related items together. No adjectives or promotional language."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.4,
                max_tokens=800
            )
            
            ai_narrative = ai_response.choices[0].message.content + "\n\n"
            
        except Exception as e:
            logger.warning(f"AI summary generation failed: {type(e).__name__}: {str(e)}", exc_info=True)
    
    report += ai_narrative
    
    # Add detailed structured project breakdown
    report += "**DETAILED PROJECT BREAKDOWN**\n\n"
    
    try:
        for proj in sorted(all_projects, key=lambda x: (x['client'], x['product'])):
            product = proj['product']
            client = proj['client']
            stories = proj['stories']
            
            try:
                # Previous sprint statuses
                done = stories.get('done_previous_sprint', [])
                in_qa = stories.get('in_qa_previous_sprint', [])
                in_development = stories.get('in_development_previous_sprint', [])
                in_design = stories.get('in_design_previous_sprint', [])
                in_review = stories.get('in_review_previous_sprint', [])
                blocked = stories.get('blocked_previous_sprint', [])
                in_transfer = stories.get('in_transfer_previous_sprint', [])
                in_progress_status = stories.get('in_progress_previous_sprint', [])
                
                # Current sprint planned
                planned = stories.get('planned_current_sprint', [])
                
                # Combine in-progress from previous sprint
                in_progress = in_qa + in_development + in_design + in_review + in_progress_status
                
                report += f"\n**{client} - {product}** (This Sprint: Done {len(done)}, In Progress {len(in_progress)}, Blocked {len(blocked)} | Upcoming: Planned {len(planned)})\n"
                report += f"{'-' * 65}\n"
                
                report += f"\n📅 **THIS SPRINT RESULTS:**\n"
                
                report += f"\n✅ **Completed:** {len(done)} tasks\n"
                if done:
                    for s in done:
                        try:
                            report += f"  • {s.get('name', 'Unknown task')}\n"
                        except Exception as e:
                            logger.warning(f"    Error adding done task: {str(e)}")
                            report += f"  • [Error reading task]\n"
                else:
                    report += "  • No tasks completed\n"
                
                if in_qa:
                    report += f"\n🔍 **In QA:** {len(in_qa)} tasks\n"
                    for s in in_qa:
                        try:
                            report += f"  • {s.get('name', 'Unknown task')}\n"
                        except Exception as e:
                            logger.warning(f"    Error adding QA task: {str(e)}")
                
                if in_development:
                    report += f"\n🔨 **In Development:** {len(in_development)} tasks\n"
                    for s in in_development:
                        try:
                            report += f"  • {s.get('name', 'Unknown task')}\n"
                        except Exception as e:
                            logger.warning(f"    Error adding dev task: {str(e)}")
                
                if in_design:
                    report += f"\n🎨 **In Design:** {len(in_design)} tasks\n"
                    for s in in_design:
                        try:
                            report += f"  • {s.get('name', 'Unknown task')}\n"
                        except Exception as e:
                            logger.warning(f"    Error adding design task: {str(e)}")
                
                if in_review:
                    report += f"\n📝 **In Review:** {len(in_review)} tasks\n"
                    for s in in_review:
                        try:
                            report += f"  • {s.get('name', 'Unknown task')}\n"
                        except Exception as e:
                            logger.warning(f"    Error adding review task: {str(e)}")
                
                if blocked:
                    report += f"\n🚫 **Blocked:** {len(blocked)} tasks\n"
                    for s in blocked:
                        try:
                            report += f"  • {s.get('name', 'Unknown task')}\n"
                        except Exception as e:
                            logger.warning(f"    Error adding blocked task: {str(e)}")
                
                if in_transfer:
                    report += f"\n🔄 **Transferred:** {len(in_transfer)} tasks\n"
                    for s in in_transfer:
                        try:
                            report += f"  • {s.get('name', 'Unknown task')}\n"
                        except Exception as e:
                            logger.warning(f"    Error adding transfer task: {str(e)}")
                
                report += f"\n📋 **UPCOMING SPRINT PLAN:**\n"
                
                report += f"\n  📌 **Planned** ({len(planned)} items):\n"
                if planned:
                    for s in planned:
                        try:
                            report += f"    - {s.get('name', 'Unknown task')}\n"
                        except Exception as e:
                            logger.warning(f"    Error adding planned task: {str(e)}")
                else:
                    report += f"    - No items planned\n"
                
                # Summary for project
                report += f"\n  **Project Summary:** "
                summary_parts = []
                if len(done) > 0:
                    summary_parts.append(f"{len(done)} completed this sprint")
                if len(in_progress) > 0:
                    summary_parts.append(f"{len(in_progress)} were in progress")
                if len(blocked) > 0:
                    summary_parts.append(f"{len(blocked)} blocked")
                if len(in_transfer) > 0:
                    summary_parts.append(f"{len(in_transfer)} transferred")
                if len(planned) > 0:
                    summary_parts.append(f"{len(planned)} planned for upcoming sprint")
                
                report += ", ".join(summary_parts) if summary_parts else "No activity"
                report += ".\n\n"
                
            except Exception as e:
                logger.error(f"Error building breakdown for {client} - {product}: {str(e)}", exc_info=True)
                report += f"\n**{client} - {product}** - [Error generating breakdown]\n\n"
                continue
        
        return report
        
    except Exception as e:
        logger.error(f"Error building detailed breakdown: {str(e)}", exc_info=True)
        return report  # Return partial report even on error


def generate_client_summary(client, products, use_ai, report_generator):
    """Generate summary for a specific client with hours.
    
    This is for start-of-sprint reports:
    - Previous sprint: Shows tasks by status
    - Current sprint: Shows all planned tasks
    """
    report = f"📊 CLIENT SPRINT START REPORT - {client}\n"
    report += "=" * 70 + "\n\n"
    
    client_total_est_previous = 0
    client_total_act_previous = 0
    client_total_est_current = 0
    
    for product, stories in products.items():
        # Previous sprint statuses
        done = stories.get('done_previous_sprint', [])
        in_qa = stories.get('in_qa_previous_sprint', [])
        in_development = stories.get('in_development_previous_sprint', [])
        in_design = stories.get('in_design_previous_sprint', [])
        in_review = stories.get('in_review_previous_sprint', [])
        blocked = stories.get('blocked_previous_sprint', [])
        in_transfer = stories.get('in_transfer_previous_sprint', [])
        in_progress_status = stories.get('in_progress_previous_sprint', [])
        
        # Current sprint planned
        planned = stories.get('planned_current_sprint', [])
        
        # Combine in-progress from previous sprint
        in_progress = in_qa + in_development + in_design + in_review + in_progress_status
        
        # Calculate hours for this project
        all_previous_sprint = done + in_qa + in_development + in_design + in_review + blocked + in_transfer + in_progress_status
        est_previous = sum(get_hours_from_story(s, 'Estimated Effort') for s in all_previous_sprint)
        act_previous = sum(get_hours_from_story(s, 'Actual Effort') for s in all_previous_sprint)
        est_current = sum(get_hours_from_story(s, 'Estimated Effort') for s in planned)
        
        client_total_est_previous += est_previous
        client_total_act_previous += act_previous
        client_total_est_current += est_current
        
        report += f"\n\n\n**======== Project {product} (This Sprint: Done {len(done)}, In Progress {len(in_progress)}, Blocked {len(blocked)} | Upcoming: Planned {len(planned)}) ============**\n"
        
        report += f"\n📅 **THIS SPRINT:**\n"
        report += f"✅ Completed tasks:\n"
        if done:
            for s in done:
                report += f"  • {s['name']}\n"
        else:
            report += "  • No tasks completed\n"
        
        if in_qa:
            report += f"\n🔍 In QA:\n"
            for s in in_qa:
                report += f"  • {s['name']}\n"
        
        if in_development:
            report += f"\n🔨 In Development:\n"
            for s in in_development:
                report += f"  • {s['name']}\n"
        
        if in_design:
            report += f"\n🎨 In Design:\n"
            for s in in_design:
                report += f"  • {s['name']}\n"
        
        if in_review:
            report += f"\n📝 In Review:\n"
            for s in in_review:
                report += f"  • {s['name']}\n"
        
        if blocked:
            report += f"\n🚫 Blocked:\n"
            for s in blocked:
                report += f"  • {s['name']}\n"
        
        if in_transfer:
            report += f"\n🔄 Transferred:\n"
            for s in in_transfer:
                report += f"  • {s['name']}\n"
        
        report += f"\n📋 **UPCOMING SPRINT PLAN:**\n"
        
        # Planned tasks
        if planned:
            report += f"\n  📌 Planned ({len(planned)} items):\n"
            for s in planned:
                report += f"    - {s['name']}\n"
        else:
            report += "  • No tasks planned\n"
        
        # Detailed summary
        report += f"\n💬 Summary: "
        summary_parts = []
        if len(done) > 0:
            summary_parts.append(f"{len(done)} tasks completed this sprint")
        if len(in_progress) > 0:
            summary_parts.append(f"{len(in_progress)} were in progress")
        if len(blocked) > 0:
            summary_parts.append(f"{len(blocked)} blocked")
        if len(in_transfer) > 0:
            summary_parts.append(f"{len(in_transfer)} transferred")
        if len(planned) > 0:
            summary_parts.append(f"{len(planned)} planned for upcoming sprint")
        report += ", ".join(summary_parts) + ".\n"
        
        # Hours for this project
        report += f"\n⏱️ Hours:\n"
        report += f"  This sprint: Est.: {est_previous:.1f} hours, Actual: {act_previous:.1f} hours\n"
        report += f"  Upcoming sprint: Est.: {est_current:.1f} hours\n"
    
    # Client total summary
    total_done = sum(len(p.get('done_previous_sprint', [])) for p in products.values())
    total_in_qa = sum(len(p.get('in_qa_previous_sprint', [])) for p in products.values())
    total_in_development = sum(len(p.get('in_development_previous_sprint', [])) for p in products.values())
    total_in_design = sum(len(p.get('in_design_previous_sprint', [])) for p in products.values())
    total_in_review = sum(len(p.get('in_review_previous_sprint', [])) for p in products.values())
    total_blocked = sum(len(p.get('blocked_previous_sprint', [])) for p in products.values())
    total_in_transfer = sum(len(p.get('in_transfer_previous_sprint', [])) for p in products.values())
    total_in_progress = sum(len(p.get('in_progress_previous_sprint', [])) for p in products.values())
    total_planned = sum(len(p.get('planned_current_sprint', [])) for p in products.values())
    
    total_in_progress_all = total_in_qa + total_in_development + total_in_design + total_in_review + total_in_progress
    
    report += f"\n{'='*70}\n"
    report += f"📌 TOTAL SUMMARY FOR {client}:\n"
    report += f"  This sprint: {total_done} done, {total_in_progress_all} in progress, {total_blocked} blocked, {total_in_transfer} transferred ({client_total_act_previous:.1f}h actual).\n"
    report += f"  Upcoming sprint: {total_planned} tasks planned ({client_total_est_current:.1f}h estimated).\n"
    
    return {
        'client': client,
        'summary': report,
        'total_done': total_done,
        'total_in_progress': total_in_progress_all,
        'total_blocked': total_blocked,
        'total_planned': total_planned,
        'hours_previous_estimated': client_total_est_previous,
        'hours_previous_actual': client_total_act_previous,
        'hours_current_estimated': client_total_est_current
    }


# ==================== COLUMN SYNC HELPER FUNCTIONS ====================

# Board name patterns for identification
BOARD_PATTERNS = {
    'fast_lane': ['fast lane', 'fl', 'fastlane'],
    'bugs_queue': ['bugs queue', 'bq', 'bugs', 'bug queue'],
    'user_stories': ['sprint backlog', 'user stories', 'tasks/user stories', 'user stories & tasks'],
    'incoming_request': ['incoming request', 'incoming requests'],
    'main_board': ['main board', 'main'],
}

# Column mapping between boards (column titles that should be synced)
# These are the common columns that exist in all three boards
# The matching is case-insensitive and supports partial matching
SYNCABLE_COLUMNS = [
    # Status columns
    'Priority',
    'Status',
    'Severity',
    'Category',
    'Type',
    'Product',  # Product/Project dropdown
    'Products',  # Plural variant used in User Stories board
    'Customers', # Manual customer column (editable, non-mirrored)
    'Billable',  # Billable status column
    # People columns
    'Owner',
    'Person',
    'Assignee',
    'Assigned',
    'Developer',
    'Designer',
    'QA',
    'Tester',
    'Reportter',  # BQ board spelling - maps to Owner in User Stories
    'Reporter',   # Correct spelling variant
    # Date columns
    'Timeline',
    'Due Date',
    'Due',
    'Deadline',
    'Start Date',
    'End Date',
    'Date',
    'Task due date',
    # Tag/Label columns
    'Tags',
    'Labels',
    # Effort/Estimation columns
    'Estimation',
    'Estimated',
    'Effort',
    'Hours',
    'Story Points',
    'Points',
    # Text columns
    'Description',
    'Notes',
    'Comments',
    # Dropdown columns (common names)
    'Environment',
    'Platform',
    'Component',
    'Module',
    'Sprint',
    'Version',
    'Release',
    'Browser',
    'Device',
    'OS',
    'Resolution',
    # Number columns
    'Progress',
    'Percentage',
    '%',
    # Board relation / Link columns
    'Connected Epics',
    'Epics',
    'Epic',
    'Connected Items',
    'Linked Items',
    'Parent',
    'Dependencies',
    'Depends On',
    'Blocked By',
    'Blocks',
    # Duration/Time tracking columns
    'Duration in Status',
    'Duration',
    'Time in Status',
    # Link/URL columns
    'GitHub link',
    'GitHub',
    'Link',
    'URL',
]

# Link column names in User Stories board
US_LINK_COLUMNS = {
    'fast_lane': ['Fast Lane', 'FL', 'Fast Lane Link', 'FL Link'],
    'bugs_queue': ['Bugs Queue', 'BQ', 'Bugs Queue Link', 'BQ Link', 'Bug Queue'],
    'incoming_request': ['Incoming Request', 'IR', 'Incoming Request Link', 'IR Link', 'Incoming Requests']
}

# Link column names in FL/BQ/IR boards pointing to User Stories
FL_BQ_LINK_COLUMNS = ['User Story', 'User Stories', 'US', 'User Story Link', 'US Link', 'Task', 'Tasks']

# Link column names in Incoming Request board pointing to User Stories (same pattern)
IR_LINK_COLUMNS = FL_BQ_LINK_COLUMNS

# Status values that trigger item creation in User Stories board (instead of sync)
MOVE_TO_SPRINTS_STATUS_VALUES = [
    "move to 'sprints'",
    "move to sprints",
    "move to sprint",
    "sprints",
    "sprint",
]


def is_move_to_sprints_status(status_value: str) -> bool:
    """
    Check if a status value indicates the item should be moved to Sprints/User Stories board.
    
    Args:
        status_value: The status label text
        
    Returns:
        True if this is a "Move to Sprints" status
    """
    if not status_value:
        return False
    
    status_lower = status_value.lower().strip()
    
    for move_status in MOVE_TO_SPRINTS_STATUS_VALUES:
        if move_status in status_lower or status_lower in move_status:
            return True
    
    return False


# Status values in Incoming Request board that trigger item creation in User Stories
IR_SPRINT_STATUS_VALUES = ['sprint', 'in sprint', 'to sprint']


def is_ir_sprint_status(status_value: str) -> bool:
    """
    Check if a status value on the Incoming Request board means the item
    should be created/linked in the User Stories board.

    The trigger status is 'Sprint' (or variants).

    Args:
        status_value: The status label text from the IR board

    Returns:
        True if this status should trigger User Stories creation
    """
    if not status_value:
        return False
    status_lower = status_value.lower().strip()
    return status_lower in IR_SPRINT_STATUS_VALUES


# Status values that trigger the 'In Transfer' duplicate-to-next-sprint logic
IN_TRANSFER_STATUS_VALUES = ['in transfer', 'in-transfer', 'transfer']


def is_in_transfer_status(status_value: str) -> bool:
    """
    Check if a status value represents 'In Transfer', which should trigger
    duplicating the item to the latest (newly created) sprint.

    Args:
        status_value: The status label text

    Returns:
        True if this is an 'In Transfer' status
    """
    if not status_value:
        return False
    status_lower = status_value.lower().strip()
    return status_lower in IN_TRANSFER_STATUS_VALUES


def extract_status_label_from_value(value: dict) -> str:
    """
    Extract the status label text from a column value object.
    
    Args:
        value: The column value dict (can be nested in various formats)
        
    Returns:
        The status label text, or empty string if not found
    """
    if not value:
        return ''
    
    # If value is a string, try to parse it as JSON
    if isinstance(value, str):
        try:
            import json
            value = json.loads(value)
        except:
            return value  # Return the string itself
    
    if not isinstance(value, dict):
        return str(value) if value else ''
    
    # Format 1: {"label": {"text": "Status Name", ...}}
    if 'label' in value:
        label = value['label']
        if isinstance(label, dict):
            return label.get('text', '')
        elif isinstance(label, str):
            return label
    
    # Format 2: {"text": "Status Name"}
    if 'text' in value:
        return value['text']
    
    # Format 3: Look for any 'label' key in nested structure
    for key, val in value.items():
        if isinstance(val, dict) and 'text' in val:
            return val['text']
    
    return ''


def identify_board_type(board_name: str) -> str:
    """
    Identify the type of board based on its name.
    
    Args:
        board_name: Name of the board
        
    Returns:
        Board type: 'fast_lane', 'bugs_queue', 'user_stories', or 'unknown'
    """
    board_name_lower = board_name.lower()
    
    for board_type, patterns in BOARD_PATTERNS.items():
        for pattern in patterns:
            if pattern in board_name_lower:
                return board_type
    
    return 'unknown'


def get_link_column_id(columns: list, source_board_type: str, target_board_type: str) -> str:
    """
    Get the link column ID that connects source board to target board.
    
    Args:
        columns: List of column definitions from the board
        source_board_type: Type of the source board
        target_board_type: Type of the target board
        
    Returns:
        Column ID for the link column, or None if not found
    """
    # Determine which patterns to look for
    if source_board_type == 'user_stories':
        # Looking for FL, BQ, or IR link columns in User Stories board
        if target_board_type == 'fast_lane':
            patterns = US_LINK_COLUMNS['fast_lane']
        elif target_board_type == 'bugs_queue':
            patterns = US_LINK_COLUMNS['bugs_queue']
        elif target_board_type == 'incoming_request':
            patterns = US_LINK_COLUMNS['incoming_request']
        else:
            return None
    elif source_board_type in ['fast_lane', 'bugs_queue']:
        # Looking for User Story link column in FL/BQ board
        patterns = FL_BQ_LINK_COLUMNS
    elif source_board_type == 'incoming_request':
        # Looking for User Story link column in IR board
        patterns = IR_LINK_COLUMNS
    else:
        return None
    
    # Find matching column
    for col in columns:
        col_title = col.get('title', '').lower()
        col_type = col.get('type', '')
        
        # Link columns are of type 'board_relation'
        if col_type == 'board_relation':
            for pattern in patterns:
                if pattern.lower() in col_title or col_title in pattern.lower():
                    return col.get('id')
    
    return None


# Common abbreviations mapping for column name normalization
COLUMN_ABBREVIATIONS = {
    'hrs': 'hours',
    'hr': 'hour',
    'est': 'estimated',
    'est.': 'estimated',
    'act': 'actual',
    'act.': 'actual',
    'desc': 'description',
    'desc.': 'description',
    'num': 'number',
    'num.': 'number',
    'qty': 'quantity',
    'qty.': 'quantity',
    'pct': 'percent',
    'pct.': 'percent',
    '%': 'percent',
    'dev': 'development',
    'dev.': 'development',
    'prod': 'product',
    'prod.': 'product',
    'proj': 'project',
    'proj.': 'project',
    'info': 'information',
    'info.': 'information',
    'req': 'request',
    'req.': 'request',
    'approx': 'approximate',
    'approx.': 'approximate',
    'orig': 'original',
    'orig.': 'original',
    'init': 'initial',
    'init.': 'initial',
    'curr': 'current',
    'curr.': 'current',
    'prev': 'previous',
    'prev.': 'previous',
}

# Cross-board column name mapping for columns with different names that should sync together
# Maps normalized column names to their equivalents
CROSS_BOARD_COLUMN_MAPPING = {
    'owner': ['reporter', 'reportter', 'person'],  # User Stories Owner syncs with BQ Reporter and FL Person
    'reporter': ['owner'],
    'reportter': ['owner'],  # BQ misspelling
    'person': ['owner'],  # FL/IR Person syncs with User Stories Owner
    'epics': ['epic'],  # FL "Epics" syncs with User Stories "Epic"
    'epic': ['epics'],
    'customers': ['customer', 'client', 'clients'],  # IR/US Customers column variants
    'customer': ['customers', 'client', 'clients'],
    'products': ['product'],  # IR/US Products column variants
    'product': ['products'],
}


def normalize_column_title(title: str) -> str:
    """
    Normalize a column title by expanding abbreviations and removing special characters.
    
    Args:
        title: Original column title
        
    Returns:
        Normalized title for comparison
    """
    if not title:
        return ''
    
    # Convert to lowercase and strip
    normalized = title.lower().strip()
    
    # Remove common punctuation that might differ
    normalized = normalized.replace('.', ' ').replace('_', ' ').replace('-', ' ')
    
    # Split into words
    words = normalized.split()
    
    # Expand abbreviations
    expanded_words = []
    for word in words:
        word = word.strip()
        if word in COLUMN_ABBREVIATIONS:
            expanded_words.append(COLUMN_ABBREVIATIONS[word])
        else:
            # Also check with period added
            if word + '.' in COLUMN_ABBREVIATIONS:
                expanded_words.append(COLUMN_ABBREVIATIONS[word + '.'])
            else:
                expanded_words.append(word)
    
    # Rejoin and remove extra spaces
    result = ' '.join(expanded_words)
    
    return result


def find_matching_column(source_columns: list, target_columns: list, source_column_id: str) -> dict:
    """
    Find the matching column in the target board for a source column.
    Matches by column title/name since IDs can be random strings across boards.
    Uses normalization to handle abbreviations (e.g., "Hrs" vs "Hours").
    
    Args:
        source_columns: List of column definitions from source board
        target_columns: List of column definitions from target board
        source_column_id: ID of the column that changed
        
    Returns:
        Dict with 'id' and 'title' of matching column, or None if not found
    """
    # First, find the source column info by ID
    source_col = None
    for col in source_columns:
        if col.get('id') == source_column_id:
            source_col = col
            break
    
    if not source_col:
        logger.warning(f"Source column with ID '{source_column_id}' not found in source columns list")
        return None
    
    source_title = source_col.get('title', '').lower().strip()
    source_title_normalized = normalize_column_title(source_col.get('title', ''))
    source_type = source_col.get('type', '')
    
    logger.info(f"Looking for matching column: title='{source_col.get('title')}', normalized='{source_title_normalized}', type='{source_type}'")
    
    # Check if this column should be synced based on SYNCABLE_COLUMNS
    should_sync = False
    for syncable in SYNCABLE_COLUMNS:
        syncable_normalized = normalize_column_title(syncable)
        if syncable_normalized in source_title_normalized or source_title_normalized in syncable_normalized:
            should_sync = True
            break
        # Also check original names
        if syncable.lower() in source_title or source_title in syncable.lower():
            should_sync = True
            break
    
    if not should_sync:
        logger.info(f"Column '{source_col.get('title')}' is not in syncable columns list")
        return None
    
    # Strategy 1: Match by exact normalized title and same type
    for col in target_columns:
        target_title_normalized = normalize_column_title(col.get('title', ''))
        target_type = col.get('type', '')
        
        if target_title_normalized == source_title_normalized and target_type == source_type:
            logger.info(f"Found exact normalized match: '{col.get('title')}' (ID: {col.get('id')})")
            return {
                'id': col.get('id'),
                'title': col.get('title'),
                'type': col.get('type')
            }
    
    # Strategy 2: Match by exact normalized title, flexible on type
    for col in target_columns:
        target_title_normalized = normalize_column_title(col.get('title', ''))
        
        if target_title_normalized == source_title_normalized:
            logger.info(f"Found normalized title match (different type): '{col.get('title')}' (ID: {col.get('id')}, type: {col.get('type')} vs {source_type})")
            return {
                'id': col.get('id'),
                'title': col.get('title'),
                'type': col.get('type')
            }
    
    # Strategy 3: Match by exact original title (case insensitive) and same type
    for col in target_columns:
        target_title = col.get('title', '').lower().strip()
        target_type = col.get('type', '')
        
        if target_title == source_title and target_type == source_type:
            logger.info(f"Found exact match: '{col.get('title')}' (ID: {col.get('id')})")
            return {
                'id': col.get('id'),
                'title': col.get('title'),
                'type': col.get('type')
            }
    
    # Strategy 4: Match by partial normalized title and same type
    for col in target_columns:
        target_title_normalized = normalize_column_title(col.get('title', ''))
        target_type = col.get('type', '')
        
        if (source_title_normalized in target_title_normalized or target_title_normalized in source_title_normalized) and target_type == source_type:
            logger.info(f"Found partial normalized match: '{col.get('title')}' (ID: {col.get('id')})")
            return {
                'id': col.get('id'),
                'title': col.get('title'),
                'type': col.get('type')
            }
    
    # Strategy 5: Match by partial normalized title, flexible on type
    for col in target_columns:
        target_title_normalized = normalize_column_title(col.get('title', ''))
        
        if source_title_normalized in target_title_normalized or target_title_normalized in source_title_normalized:
            logger.info(f"Found partial normalized match (different type): '{col.get('title')}' (ID: {col.get('id')})")
            return {
                'id': col.get('id'),
                'title': col.get('title'),
                'type': col.get('type')
            }
    
    # Strategy 6: Match by partial original title and same type
    for col in target_columns:
        target_title = col.get('title', '').lower().strip()
        target_type = col.get('type', '')
        
        if (source_title in target_title or target_title in source_title) and target_type == source_type:
            logger.info(f"Found partial match: '{col.get('title')}' (ID: {col.get('id')})")
            return {
                'id': col.get('id'),
                'title': col.get('title'),
                'type': col.get('type')
            }
    
    # Strategy 7: Match by partial original title, flexible on type
    for col in target_columns:
        target_title = col.get('title', '').lower().strip()
        
        if source_title in target_title or target_title in source_title:
            logger.info(f"Found partial title match (different type): '{col.get('title')}' (ID: {col.get('id')})")
            return {
                'id': col.get('id'),
                'title': col.get('title'),
                'type': col.get('type')
            }
    
    # Strategy 8: Match by cross-board column mapping (e.g., "Reportter" <-> "Owner")
    # Check if the source column has any mapped equivalents
    for col in target_columns:
        target_title = col.get('title', '').lower().strip()
        target_type = col.get('type', '')
        
        # Check if source title has mapped equivalents
        if source_title in CROSS_BOARD_COLUMN_MAPPING:
            equivalents = CROSS_BOARD_COLUMN_MAPPING[source_title]
            if target_title in equivalents:
                logger.info(f"Found cross-board mapping match: '{source_col.get('title')}' -> '{col.get('title')}' (ID: {col.get('id')})")
                return {
                    'id': col.get('id'),
                    'title': col.get('title'),
                    'type': col.get('type')
                }
        
        # Also check with normalized titles
        if source_title_normalized in CROSS_BOARD_COLUMN_MAPPING:
            equivalents = CROSS_BOARD_COLUMN_MAPPING[source_title_normalized]
            target_title_normalized = normalize_column_title(col.get('title', ''))
            if target_title_normalized in equivalents or target_title in equivalents:
                logger.info(f"Found cross-board mapping match (normalized): '{source_col.get('title')}' -> '{col.get('title')}' (ID: {col.get('id')})")
                return {
                    'id': col.get('id'),
                    'title': col.get('title'),
                    'type': col.get('type')
                }
    
    # Log available target columns for debugging
    target_col_names = [f"{c.get('title')} ({c.get('type')})" for c in target_columns]
    logger.warning(f"No matching column found for '{source_col.get('title')}' ({source_type}). Available target columns: {target_col_names}")
    
    return None


def extract_linked_item_ids(column_values: list, link_column_id: str) -> list:
    """
    Extract linked item IDs from column values.
    
    Args:
        column_values: List of column value objects
        link_column_id: ID of the link column
        
    Returns:
        List of linked item IDs
    """
    for cv in column_values:
        if cv.get('id') == link_column_id:
            linked_ids = cv.get('linked_item_ids', [])
            if linked_ids:
                return [str(lid) for lid in linked_ids]
            
            # Fallback: try to parse from value field
            value = cv.get('value')
            if value:
                try:
                    import json
                    parsed = json.loads(value)
                    if isinstance(parsed, dict) and 'linkedPulseIds' in parsed:
                        return [str(item.get('linkedPulseId')) for item in parsed['linkedPulseIds']]
                except:
                    pass
    
    return []


def get_column_value_for_sync(column_values: list, column_id: str) -> dict:
    """
    Get the column value object for a specific column.
    
    Args:
        column_values: List of column value objects
        column_id: ID of the column
        
    Returns:
        Dict with 'value' and 'text' of the column
    """
    for cv in column_values:
        if cv.get('id') == column_id:
            return {
                'value': cv.get('value'),
                'text': cv.get('text', ''),
                'type': cv.get('type', '')
            }
    return None


def format_column_value_for_update(value: str, column_type: str) -> str:
    """
    Format a column value for the Monday.com API update mutation.
    
    Args:
        value: The raw value string from the source column
        column_type: The type of the column
        
    Returns:
        JSON string formatted for Monday.com API
    """
    import json
    
    if not value:
        return json.dumps({})
    
    # For most column types, we can use the value directly
    # The value is already in the correct JSON format from Monday.com
    try:
        # If it's already valid JSON, use it directly
        parsed = json.loads(value)
        return value
    except (json.JSONDecodeError, TypeError):
        # If not JSON, wrap it based on column type
        if column_type == 'text':
            return json.dumps(value)
        elif column_type == 'status':
            return json.dumps({'label': value})
        elif column_type == 'dropdown':
            return json.dumps({'labels': [value]})
        else:
            return json.dumps(value)


def find_matching_subitem(source_subitem_name: str, target_subitems: list) -> dict:
    """
    Find a matching subitem in the target item's subitems.
    
    Strategy:
    1. Exact name match
    2. Similar name match (contains or is contained)
    3. Position-based match (same index in list)
    
    Args:
        source_subitem_name: Name of the source subitem
        target_subitems: List of target subitems
        
    Returns:
        Matching subitem dict or None
    """
    if not target_subitems:
        return None
    
    # Strategy 1: Exact name match
    for subitem in target_subitems:
        if subitem.get('name') == source_subitem_name:
            return subitem
    
    # Strategy 2: Similar name match
    source_name_lower = source_subitem_name.lower()
    for subitem in target_subitems:
        target_name_lower = subitem.get('name', '').lower()
        if source_name_lower in target_name_lower or target_name_lower in source_name_lower:
            return subitem
    
    return None


def build_sync_response(success: bool, message: str, details: dict = None) -> dict:
    """
    Build a standardized sync response.
    
    Args:
        success: Whether the sync was successful
        message: Human-readable message
        details: Additional details dict
        
    Returns:
        Standardized response dict
    """
    response = {
        'success': success,
        'message': message
    }
    
    if details:
        response['details'] = details
    
    return response
