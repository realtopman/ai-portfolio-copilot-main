"""
OpenAI integration module for generating AI-powered report summaries.
"""

from openai import OpenAI
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ReportGenerator:
    """Generates AI-powered sprint reports using OpenAI."""
    
    def __init__(self, api_key: str, model: str = 'gpt-3.5-turbo'):
        """
        Initialize the report generator.
        
        Args:
            api_key: OpenAI API key
            model: OpenAI model to use
        """
        self.client = OpenAI(api_key=api_key)
        self.model = model
    
    def generate_report_summary(self, completed_stories: List[Dict], 
                               planned_stories: List[Dict],
                               customer_name: str = "Our Team") -> str:
        """
        Generate a comprehensive sprint report summary using OpenAI.
        
        Args:
            completed_stories: List of completed user stories
            planned_stories: List of planned user stories
            customer_name: Name of the customer for personalization
            
        Returns:
            Generated report summary
        """
        completed_list = self._format_stories_list(completed_stories)
        planned_list = self._format_stories_list(planned_stories)
        
        prompt = f"""Generate a professional and concise sprint development report for a customer project manager.

Customer: {customer_name}
Report Type: Sprint Status Update

Completed This Week:
{completed_list if completed_list else "No items completed this week."}

Planned for Next Week:
{planned_list if planned_list else "No items planned for next week."}

Please create a summary that:
1. Highlights key accomplishments
2. Identifies any epics where significant progress was made
3. Lists upcoming priorities
4. Includes a brief assessment of overall progress
5. Is written in a professional, accessible tone suitable for a non-technical project manager

Format the response in a clear, organized way with sections and bullet points where appropriate."""
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a professional project communication specialist who creates clear, concise status reports for stakeholders."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=1000
            )
            
            summary = response.choices[0].message.content
            return summary
        except Exception as e:
            logger.error(f"Error generating report: {str(e)}")
            # Return a fallback report if OpenAI fails
            return self._generate_fallback_report(completed_stories, planned_stories, customer_name)
    
    def generate_sprint_report_monday(self, completed_previous_sprint: List[Dict],
                                     in_progress: List[Dict],
                                     todo: List[Dict],
                                     blocked: List[Dict],
                                     review: List[Dict],
                                     other: List[Dict],
                                     customer_name: str = "Our Team") -> str:
        """
        Generate a sprint report for Monday workflow (previous sprint completed + current sprint detailed status).
        
        Args:
            completed_previous_sprint: Completed stories from previous sprint
            in_progress: Stories currently in progress
            todo: Stories in todo status
            blocked: Blocked stories
            review: Stories in review/testing
            other: Stories with other status
            customer_name: Name of the customer
            
        Returns:
            Generated report summary
        """
        completed_list = self._format_stories_list(completed_previous_sprint)
        in_progress_list = self._format_stories_list(in_progress)
        todo_list = self._format_stories_list(todo)
        blocked_list = self._format_stories_list(blocked)
        review_list = self._format_stories_list(review)
        other_list = self._format_stories_list(other)
        
        prompt = f"""Generate a professional and concise sprint development report for a customer project manager.

Customer: {customer_name}
Report Type: Sprint Status Update (Called at sprint start)

COMPLETED PREVIOUS SPRINT:
{completed_list if completed_list else "No items completed previous sprint."}

CURRENT WEEK STATUS:

In Progress:
{in_progress_list if in_progress_list else "No items in progress."}

In Review/Testing:
{review_list if review_list else "No items in review."}

Blocked:
{blocked_list if blocked_list else "No blocked items."}

Todo:
{todo_list if todo_list else "No todo items."}

Other:
{other_list if other_list else "No other items."}

Please create a summary that:
1. Highlights key accomplishments from last week
2. Provides clear status on current week's work
3. Calls out any blocked items that need attention
4. Identifies progress across epics
5. Is written in a professional, accessible tone suitable for a non-technical project manager

Format the response in a clear, organized way with sections and bullet points where appropriate."""
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a professional project communication specialist who creates clear, concise status reports for stakeholders."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=1200
            )
            
            summary = response.choices[0].message.content
            return summary
        except Exception as e:
            logger.error(f"Error generating Monday report: {str(e)}")
            # Return a fallback report if OpenAI fails
            return self._generate_fallback_report_monday(
                completed_previous_sprint, in_progress, todo, blocked, review, other, customer_name
            )
    
    def generate_sprint_report_simple(self, done_previous_sprint: List[Dict],
                                     in_transfer_previous_sprint: List[Dict],
                                     in_progress_previous_sprint: List[Dict],
                                     blocked_previous_sprint: List[Dict],
                                     planned_current_sprint: List[Dict],
                                     customer_name: str = "Our Team",
                                     hours_by_type_table: Optional[List[Dict]] = None) -> str:
        """
        Generate a simplified sprint report with clear structure (called at sprint start).
        
        Args:
            done_previous_sprint: Done items from previous sprint
            in_transfer_previous_sprint: Stories in transfer from previous sprint
            in_progress_previous_sprint: Stories in progress from previous sprint
            blocked_previous_sprint: Blocked stories from previous sprint (shown if present)
            planned_current_sprint: Planned items for current sprint
            customer_name: Name of the customer
            hours_by_type_table: Optional table rows with hours grouped by type
            
        Returns:
            Generated report summary
        """

        
        # Ensure all parameters are lists
        done_previous_sprint = done_previous_sprint if isinstance(done_previous_sprint, list) else []

        in_transfer_previous_sprint = in_transfer_previous_sprint if isinstance(in_transfer_previous_sprint, list) else []

        in_progress_previous_sprint = in_progress_previous_sprint if isinstance(in_progress_previous_sprint, list) else []

        blocked_previous_sprint = blocked_previous_sprint if isinstance(blocked_previous_sprint, list) else []

        planned_current_sprint = planned_current_sprint if isinstance(planned_current_sprint, list) else []
        
        try:
            done_list = self._format_stories_with_subitems(done_previous_sprint)
        except Exception as e:
            logger.error(f"  ✗ Error formatting done_list: {str(e)}", exc_info=True)
            done_list = ""
        
        try:
            in_progress_list = self._format_stories_with_subitems(in_progress_previous_sprint)
        except Exception as e:
            logger.error(f"  ✗ Error formatting in_progress_list: {str(e)}", exc_info=True)
            in_progress_list = ""

        try:
            in_transfer_list = self._format_stories_with_subitems(in_transfer_previous_sprint)
        except Exception as e:
            logger.error(f"  ✗ Error formatting in_transfer_list: {str(e)}", exc_info=True)
            in_transfer_list = ""
        
        try:
            planned_list = self._format_stories_with_subitems(planned_current_sprint)
        except Exception as e:
            logger.error(f"  ✗ Error formatting planned_list: {str(e)}", exc_info=True)
            planned_list = ""
        
        try:
            blocked_list = self._format_stories_with_subitems(blocked_previous_sprint) if blocked_previous_sprint else None
        except Exception as e:
            logger.error(f"  ✗ Error formatting blocked_list: {str(e)}", exc_info=True)
            blocked_list = None
        
        # Calculate hours
        def get_hours(story, field_name):
            try:
                if not isinstance(story, dict):
                    return 0.0
                    
                if 'columns_by_name' in story:
                    value = story['columns_by_name'].get(field_name, {})
                    if isinstance(value, dict):
                        text_val = value.get('text', '0')
                    else:
                        text_val = str(value) if value else '0'
                    result = float(text_val) if text_val else 0.0
                    return result
                    
                value = story.get(field_name, 0)
                result = float(value) if value else 0.0
                return result
            except (ValueError, TypeError, AttributeError) as e:
                return 0.0
        
        try:
            estimated_previous_sprint = sum(get_hours(s, 'Estimated Effort') for s in done_previous_sprint)
        except Exception as e:
            logger.error(f"  ✗ Error calculating estimated_previous_sprint: {str(e)}", exc_info=True)
            estimated_previous_sprint = 0.0
        
        try:
            actual_previous_sprint = sum(get_hours(s, 'Actual Effort') for s in done_previous_sprint)
        except Exception as e:
            logger.error(f"  ✗ Error calculating actual_previous_sprint: {str(e)}", exc_info=True)
            actual_previous_sprint = 0.0
        
        try:
            estimated_current_sprint = sum(get_hours(s, 'Estimated Effort') for s in planned_current_sprint + in_progress_previous_sprint + in_transfer_previous_sprint)
        except Exception as e:
            logger.error(f"  ✗ Error calculating estimated_current_sprint: {str(e)}", exc_info=True)
            estimated_current_sprint = 0.0

        blocked_section = f"""
Blocked Items from This Sprint (NEEDS ATTENTION):
{blocked_list}
""" if blocked_previous_sprint else ""

        # Build hours summary - use table format if provided, otherwise use simple summary
        if hours_by_type_table and len(hours_by_type_table) > 1:
            # Format hours table as text table for the prompt
            hours_summary = "\nHOURS SUMMARY BY TYPE:\n"
            hours_summary += "=" * 80 + "\n\n"
            
            # Build table header
            header_cells = hours_by_type_table[0].get("cells", [])
            header_line = " | ".join([f"{cell.get('insert', ''):<25}" for cell in header_cells])
            hours_summary += header_line + "\n"
            hours_summary += "-" * 80 + "\n"
            
            # Build table rows
            for row in hours_by_type_table[1:]:
                row_cells = row.get("cells", [])
                row_line = " | ".join([f"{cell.get('insert', ''):<25}" for cell in row_cells])
                hours_summary += row_line + "\n"
            
            hours_summary += "=" * 80 + "\n\n"
            
            logger.info(f"  Hours by type table included with {len(hours_by_type_table)-1} type rows")
        else:
            # Fallback to simple summary
            hours_summary = f"""
HOURS SUMMARY:
Previous Sprint: Estimated {estimated_previous_sprint:.1f}h | Actual {actual_previous_sprint:.1f}h
Current Sprint: Estimated {estimated_current_sprint:.1f}h
"""
            logger.info(f"  Using simple hours summary (no type breakdown available)")
        prompt = f"""Generate a factual sprint report using the following data:

{hours_summary}

---

⏱️ GENERAL SUMMARY:
[Write a brief, factual summary (2-3 sentences) that states what was completed this sprint, what is planned for the upcoming sprint, and mentions any blocked items if present. Be direct and factual - avoid adjectives like "successfully", "excellent", "great progress", etc.]

---

✅ COMPLETED THIS SPRINT:
{done_list if done_list else "No items completed."}

📝 IN PROGRESS THIS SPRINT:
{in_progress_list if in_progress_list else "No items in progress."}

� PLANNED NEXT SPRINT:
{planned_list if planned_list else "No items planned."}
{blocked_section}

Please follow these requirements carefully:
1. Use EXACTLY the section headers with icons as shown above (⏱️ GENERAL SUMMARY, ✅ COMPLETED THIS SPRINT, 📝 IN PROGRESS THIS SPRINT, � PLANNED NEXT SPRINT)
2. Use --- as horizontal separator between major sections (after hours summary and after general summary)
3. Format main task items as: • **Task Name**
4. Format subitems as: ◦ *Subitem Name*
5. You MUST list EVERY SINGLE task and subtask provided above - never truncate, summarize, or skip any items
6. NEVER use placeholder phrases like "[Additional tasks...]", "[etc...]", "[See original report...]" or any similar placeholders
7. If there are 50 tasks, list all 50 tasks. If there are 100 subtasks, list all 100 subtasks
8. DO NOT add extra headers, titles, or decorative elements beyond what is shown in the template above
9. DO NOT use different emoji or formatting styles than specified
10. Keep the GENERAL SUMMARY as 2-3 sentences maximum, factual and direct
11. AVOID adjectives like: successfully, excellent, great, significant, impressive, key, important, critical

The task lists provided above are COMPLETE - you must reproduce them in full without any omissions."""
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a factual report formatter. Output ONLY the formatted report content - do not include any meta-commentary, instructions, or explanatory text. List ALL tasks and subtasks provided without any truncation, summarization, or placeholder phrases."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=4096
            )
            
            summary = response.choices[0].message.content
            return summary
        except Exception as e:
            logger.error(f"STEP 6 FAILED: Error generating simple report")
            logger.error(f"  Exception type: {type(e).__name__}")
            logger.error(f"  Exception message: {str(e)}")
            logger.error(f"  Full traceback:", exc_info=True)
            # Return a fallback report if OpenAI fails
            return self._generate_fallback_report_simple(
                done_previous_sprint,
                in_transfer_previous_sprint,
                in_progress_previous_sprint,
                planned_current_sprint,
                blocked_previous_sprint,
                customer_name
            )
    
    def _format_stories_list(self, stories: List[Dict]) -> str:
        """
        Format a list of user stories into readable text.
        
        Args:
            stories: List of user story dictionaries
            
        Returns:
            Formatted string of stories
        """
        if not stories:
            return ""
        
        formatted = []
        for story in stories:
            epic = story.get('epic', 'General')
            name = story.get('name', 'Unknown')
            formatted.append(f"- [{epic}] {name}")
        
        return "\n".join(formatted)
    
    def _format_stories_with_subitems(self, stories: List[Dict]) -> str:
        """
        Format a list of user stories with their subitems into readable text.
        
        Args:
            stories: List of user story dictionaries
            
        Returns:
            Formatted string of stories with subitems
        """
        if not stories or not isinstance(stories, list):
            return ""
        
        formatted = []
        try:
            for story in stories:
                if not isinstance(story, dict):
                    continue
                
                name = story.get('name', 'Unknown')
                formatted.append(f"• **{name}**")
                
                # Add subitems if present
                subitems = story.get('subitems', [])
                if subitems and isinstance(subitems, list):
                    for subitem in subitems:
                        if isinstance(subitem, dict):
                            subitem_name = subitem.get('name', 'Unknown')
                            formatted.append(f"  ◦ *{subitem_name}*")
        except Exception as e:
            return ""
        
        return "\n".join(formatted)
    
    def _generate_fallback_report(self, completed_stories: List[Dict], 
                                 planned_stories: List[Dict],
                                 customer_name: str) -> str:
        """
        Generate a fallback report if OpenAI fails.
        
        Args:
            completed_stories: List of completed user stories
            planned_stories: List of planned user stories
            customer_name: Name of the customer
            
        Returns:
            Fallback report text
        """
        completed_count = len(completed_stories)
        planned_count = len(planned_stories)
        
        report = f"""Weekly Development Report - {customer_name}

COMPLETED THIS WEEK ({completed_count} items):
"""
        
        if completed_stories:
            epics_completed = {}
            for story in completed_stories:
                epic = story.get('epic', 'General')
                if epic not in epics_completed:
                    epics_completed[epic] = []
                epics_completed[epic].append(story.get('name', 'Unknown'))
            
            for epic, stories in epics_completed.items():
                report += f"\n{epic}:\n"
                for story in stories:
                    report += f"  ✓ {story}\n"
        else:
            report += "\nNo items completed this week.\n"
        
        report += f"\n\nPLANNED FOR NEXT WEEK ({planned_count} items):\n"
        
        if planned_stories:
            epics_planned = {}
            for story in planned_stories:
                epic = story.get('epic', 'General')
                if epic not in epics_planned:
                    epics_planned[epic] = []
                epics_planned[epic].append(story.get('name', 'Unknown'))
            
            for epic, stories in epics_planned.items():
                report += f"\n{epic}:\n"
                for story in stories:
                    report += f"  → {story}\n"
        else:
            report += "\nNo items planned for next week.\n"
        
        report += f"\n\nSUMMARY:\nWe completed {completed_count} items this week and have {planned_count} items planned for the upcoming week."
        
        return report
    
    def _generate_fallback_report_monday(self, completed_previous_sprint: List[Dict],
                                        in_progress: List[Dict],
                                        todo: List[Dict],
                                        blocked: List[Dict],
                                        review: List[Dict],
                                        other: List[Dict],
                                        customer_name: str) -> str:
        """
        Generate a fallback report for Monday workflow.
        
        Args:
            completed_previous_sprint: Completed stories from previous sprint
            in_progress: Stories in progress
            todo: Todo stories
            blocked: Blocked stories
            review: Stories in review
            other: Other stories
            customer_name: Name of the customer
            
        Returns:
            Fallback report text
        """
        report = f"""Sprint Development Report - {customer_name}

COMPLETED PREVIOUS SPRINT ({len(completed_previous_sprint)} items):
"""
        
        if completed_previous_sprint:
            epics = {}
            for story in completed_previous_sprint:
                epic = story.get('epic', 'General')
                if epic not in epics:
                    epics[epic] = []
                epics[epic].append(story.get('name', 'Unknown'))
            
            for epic, stories in epics.items():
                report += f"\n{epic}:\n"
                for story in stories:
                    report += f"  ✓ {story}\n"
        else:
            report += "\nNo items completed last week.\n"
        
        report += f"\n\nCURRENT WEEK STATUS:\n"
        
        if in_progress:
            report += f"\nIn Progress ({len(in_progress)} items):\n"
            for story in in_progress:
                report += f"  🔨 {story.get('name', 'Unknown')}\n"
        
        if review:
            report += f"\nIn Review/Testing ({len(review)} items):\n"
            for story in review:
                report += f"  👀 {story.get('name', 'Unknown')}\n"
        
        if blocked:
            report += f"\nBlocked - Needs Attention ({len(blocked)} items):\n"
            for story in blocked:
                report += f"  🚧 {story.get('name', 'Unknown')}\n"
        
        if todo:
            report += f"\nTodo ({len(todo)} items):\n"
            for story in todo[:5]:  # Limit to first 5
                report += f"  📋 {story.get('name', 'Unknown')}\n"
            if len(todo) > 5:
                report += f"  ... and {len(todo) - 5} more\n"
        
        report += f"\n\nSUMMARY:\nCompleted {len(completed_previous_sprint)} items last week. "
        report += f"Current week: {len(in_progress)} in progress, {len(review)} in review"
        if blocked:
            report += f", {len(blocked)} blocked"
        report += "."
        
        return report
    
    def _generate_fallback_report_simple(self, done_previous_sprint: List[Dict],
                                        in_transfer_previous_sprint: List[Dict],
                                        in_progress_previous_sprint: List[Dict],
                                        planned_current_sprint: List[Dict],
                                        blocked_previous_sprint: List[Dict],
                                        customer_name: str) -> str:
        """
        Generate a simple, well-structured fallback report.
        
        Args:
            done_previous_sprint: Done items from previous sprint
            in_transfer_previous_sprint: Items in transfer from previous sprint
            in_progress_previous_sprint: Items in progress from previous sprint
            planned_current_sprint: Planned items for current sprint
            blocked_previous_sprint: Blocked items from previous sprint
            customer_name: Name of the customer
            
        Returns:
            Fallback report text
        """
        # Ensure all parameters are lists
        done_previous_sprint = done_previous_sprint if isinstance(done_previous_sprint, list) else []
        in_transfer_previous_sprint = in_transfer_previous_sprint if isinstance(in_transfer_previous_sprint, list) else []
        in_progress_previous_sprint = in_progress_previous_sprint if isinstance(in_progress_previous_sprint, list) else []
        planned_current_sprint = planned_current_sprint if isinstance(planned_current_sprint, list) else []
        blocked_previous_sprint = blocked_previous_sprint if isinstance(blocked_previous_sprint, list) else []
        
        # Calculate hours
        def get_hours(story, field_name):
            try:
                if not isinstance(story, dict):
                    return 0.0
                    
                if 'columns_by_name' in story:
                    value = story['columns_by_name'].get(field_name, {})
                    if isinstance(value, dict):
                        text_val = value.get('text', '0')
                    else:
                        text_val = str(value) if value else '0'
                    return float(text_val) if text_val else 0.0
                    
                value = story.get(field_name, 0)
                return float(value) if value else 0.0
            except (ValueError, TypeError, AttributeError):
                return 0.0
        
        estimated_previous_sprint = sum(get_hours(s, 'Estimated Effort') for s in done_previous_sprint)
        actual_previous_sprint = sum(get_hours(s, 'Actual Effort') for s in done_previous_sprint)
        estimated_current_sprint = sum(get_hours(s, 'Estimated Effort') for s in in_progress_previous_sprint + planned_current_sprint + in_transfer_previous_sprint)
        
        # Start report
        report = ""
        
        # Hours Summary with bold
        report += f"**⏱️ HOURS SUMMARY**\n\n"
        report += f"*This Sprint:* Estimated **{estimated_previous_sprint:.1f}h** | Actual **{actual_previous_sprint:.1f}h**\n\n"
        report += f"*Upcoming Sprint:* Estimated **{estimated_current_sprint:.1f}h**\n\n"
        report += "\n"
        
        # This Sprint Completed
        total_done = len(done_previous_sprint)
        report += f"**✅ COMPLETED THIS SPRINT ({total_done})**\n\n"
        if done_previous_sprint:
            for story in done_previous_sprint:
                report += f"  ✓ **{story.get('name', 'Unknown')}**\n"
                # Add subitems
                subitems = story.get('subitems', [])
                if subitems:
                    for subitem in subitems:
                        report += f"    • *{subitem.get('name', 'Unknown')}*\n"
        else:
            report += "  *No items completed*\n"
        report += "\n\n"
        
        # In Transfer from This Sprint
        report += f"**📦 IN TRANSFER FROM THIS SPRINT ({len(in_transfer_previous_sprint)})**\n\n"
        if in_transfer_previous_sprint:
            for story in in_transfer_previous_sprint:
                report += f"  - **{story.get('name', 'Unknown')}**\n"
                # Add subitems
                subitems = story.get('subitems', [])
                if subitems:
                    for subitem in subitems:
                        report += f"    • *{subitem.get('name', 'Unknown')}*\n"
        else:
            report += "  *No items in transfer*\n"
        report += "\n\n"

        # In Progress from This Sprint
        report += f"**🔨 IN PROGRESS FROM THIS SPRINT ({len(in_progress_previous_sprint)})**\n\n"
        if in_progress_previous_sprint:
            for story in in_progress_previous_sprint:
                report += f"  - **{story.get('name', 'Unknown')}**\n"
                # Add subitems
                subitems = story.get('subitems', [])
                if subitems:
                    for subitem in subitems:
                        report += f"    • *{subitem.get('name', 'Unknown')}*\n"
        else:
            report += "  *No items in progress*\n"
        report += "\n\n"
        
        # Upcoming Sprint Planned
        report += f"**📋 PLANNED UPCOMING SPRINT ({len(planned_current_sprint)})**\n\n"
        if planned_current_sprint:
            for story in planned_current_sprint:
                report += f"  - **{story.get('name', 'Unknown')}**\n"
                # Add subitems
                subitems = story.get('subitems', [])
                if subitems:
                    for subitem in subitems:
                        report += f"    • *{subitem.get('name', 'Unknown')}*\n"
        else:
            report += "  *No items planned*\n"
        report += "\n\n"
        
        # Blocked items (only if present)
        if blocked_previous_sprint:
            report += f"**🚧 BLOCKED FROM PREVIOUS SPRINT - NEEDS ATTENTION ({len(blocked_previous_sprint)})**\n\n"
            for story in blocked_previous_sprint:
                report += f"  ⚠️  **{story.get('name', 'Unknown')}**\n"
                # Add subitems
                subitems = story.get('subitems', [])
                if subitems:
                    for subitem in subitems:
                        report += f"    • *{subitem.get('name', 'Unknown')}*\n"
            report += "\n\n"
        
        # General Summary - Narrative style
        total_active = len(in_transfer_previous_sprint) + len(in_progress_previous_sprint) + len(planned_current_sprint)
        total_subitems_done = sum(len(s.get('subitems', [])) for s in done_previous_sprint)
        total_subitems_active = sum(len(s.get('subitems', [])) for s in in_transfer_previous_sprint + in_progress_previous_sprint + planned_current_sprint)
        
        report += f"**📌 GENERAL SUMMARY**\n\n"
        
        # Build narrative summary
        if done_previous_sprint:
            report += f"Previous sprint was productive with **{total_done} main tasks** completed ({total_subitems_done} subtasks), "
            report += f"using **{actual_previous_sprint:.1f} hours** of actual effort compared to **{estimated_previous_sprint:.1f} hours** estimated. "
            
            # Mention completed items naturally
            if total_done == 1:
                report += f"We successfully completed *{done_previous_sprint[0].get('name', 'Unknown')}*. "
            elif total_done == 2:
                report += f"We successfully completed *{done_previous_sprint[0].get('name', 'Unknown')}* and *{done_previous_sprint[1].get('name', 'Unknown')}*. "
            else:
                completed_names = [f"*{s.get('name', 'Unknown')}*" for s in done_previous_sprint[:3]]
                report += f"Key completions include {', '.join(completed_names)}"
                if total_done > 3:
                    report += f" and {total_done - 3} other tasks"
                report += ". "
        else:
            report += f"Previous sprint, no tasks were completed. "
        
        report += "\n\n"
        
        # Next sprint's work
        if total_active > 0:
            report += f"Current sprint, we have **{total_active} tasks** in the pipeline ({total_subitems_active} subtasks) "
            report += f"with an estimated effort of **{estimated_current_sprint:.1f} hours**. "
            
            if in_transfer_previous_sprint:
                report += f"We carried over "
                if len(in_transfer_previous_sprint) == 1:
                    report += f"*{in_transfer_previous_sprint[0].get('name', 'Unknown')}* from the previous sprint. "
                elif len(in_transfer_previous_sprint) == 2:
                    report += f"*{in_transfer_previous_sprint[0].get('name', 'Unknown')}* and *{in_transfer_previous_sprint[1].get('name', 'Unknown')}* from the previous sprint. "
                else:
                    transfer_names = [f"*{s.get('name', 'Unknown')}*" for s in in_transfer_previous_sprint[:2]]
                    report += f"{', '.join(transfer_names)} and {len(in_transfer_previous_sprint) - 2} other tasks from the previous sprint. "

            if in_progress_previous_sprint:
                report += f"Currently, we're actively working on "
                if len(in_progress_previous_sprint) == 1:
                    report += f"*{in_progress_previous_sprint[0].get('name', 'Unknown')}*. "
                elif len(in_progress_previous_sprint) == 2:
                    report += f"*{in_progress_previous_sprint[0].get('name', 'Unknown')}* and *{in_progress_previous_sprint[1].get('name', 'Unknown')}*. "
                else:
                    in_progress_names = [f"*{s.get('name', 'Unknown')}*" for s in in_progress_previous_sprint[:2]]
                    report += f"{', '.join(in_progress_names)} and {len(in_progress_previous_sprint) - 2} other tasks. "
            
            if planned_current_sprint:
                report += f"Additionally, we have "
                if len(planned_current_sprint) == 1:
                    report += f"*{planned_current_sprint[0].get('name', 'Unknown')}* planned for this sprint. "
                elif len(planned_current_sprint) == 2:
                    report += f"*{planned_current_sprint[0].get('name', 'Unknown')}* and *{planned_current_sprint[1].get('name', 'Unknown')}* planned. "
                else:
                    planned_names = [f"*{s.get('name', 'Unknown')}*" for s in planned_current_sprint[:2]]
                    report += f"{', '.join(planned_names)} and {len(planned_current_sprint) - 2} other tasks planned. "
        else:
            report += f"Current sprint, no tasks are currently scheduled. "
        
        # Blocked items warning
        if blocked_previous_sprint:
            report += "\n\n"
            report += f"⚠️ **IMPORTANT:** There are currently **{len(blocked_previous_sprint)} blocked tasks** from the previous sprint that require immediate attention: "
            blocked_names = [f"*{s.get('name', 'Unknown')}*" for s in blocked_previous_sprint]
            if len(blocked_previous_sprint) <= 3:
                report += ", ".join(blocked_names) + ". "
            else:
                report += ", ".join(blocked_names[:3]) + f" and {len(blocked_previous_sprint) - 3} others. "
        
        report += "\n"
        
        return report
    
    def generate_quick_summary(self, completed_stories: List[Dict], 
                              planned_stories: List[Dict]) -> str:
        """
        Generate a quick one-line summary of the sprint's work.
        
        Args:
            completed_stories: List of completed user stories
            planned_stories: List of planned user stories
            
        Returns:
            One-line summary
        """
        completed_count = len(completed_stories)
        planned_count = len(planned_stories)
        
        completed_epics = set(s.get('epic', 'General') for s in completed_stories)
        
        if completed_count > 0:
            epics_str = ', '.join(list(completed_epics)[:2])
            if len(completed_epics) > 2:
                epics_str += f", and {len(completed_epics) - 2} more"
            return f"Completed {completed_count} items ({epics_str}); {planned_count} planned for next sprint"
        else:
            return f"Planning next sprint ahead with {planned_count} items to be completed"
