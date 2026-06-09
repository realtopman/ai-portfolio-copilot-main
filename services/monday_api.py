"""
Monday.com API integration module for fetching and managing board data.
"""

import requests
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
import logging

logger = logging.getLogger(__name__)

MONDAY_API_URL = 'https://api.monday.com/v2'
DEFAULT_PAGE_LIMIT = 500


def _normalize_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


class MondayAPI:
    """Handles all interactions with Monday.com API."""
    
    def __init__(self, api_token: str, api_url: str = MONDAY_API_URL, workspace_ids: List[str] = None):
        """
        Initialize Monday API client.
        
        Args:
            api_token: Monday.com API token
            api_url: API endpoint URL
            workspace_ids: List of workspace IDs to filter boards (optional)
        """
        self.api_token = api_token
        self.api_url = api_url
        self.workspace_ids = workspace_ids or []
        self.headers = {
            'Authorization': self.api_token,
            'Content-Type': 'application/json'
        }
        logger.info(f"MondayAPI initialized with URL: {api_url}")
        if self.workspace_ids:
            logger.info(f"Filtering boards by workspace IDs: {self.workspace_ids}")
    
    def _execute_query(self, query: str, variables: Optional[Dict] = None) -> Dict:
        """
        Execute a GraphQL query against Monday.com API.
        
        Args:
            query: GraphQL query string
            variables: Variables for the query
            
        Returns:
            API response data
        """
        data = {
            'query': query,
            'variables': variables or {}
        }
        
        try:
            logger.debug(f"Executing query to {self.api_url}")
            response = requests.post(self.api_url, json=data, headers=self.headers, timeout=60)
            response.raise_for_status()
            result = response.json()
            
            if 'errors' in result:
                logger.error(f"GraphQL Error: {result['errors']}")
                raise Exception(f"GraphQL Error: {result['errors']}")
            
            return result.get('data', {})
        except requests.exceptions.RequestException as e:
            logger.error(f"API Request Error: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"API Error: {str(e)}")
            raise

    def list_boards(self, workspace_id: str) -> List[Dict[str, Any]]:
        """
        List boards in a workspace.

        This is used by the TimeBuzzer sync to resolve board names without
        duplicating Monday GraphQL client logic.
        """
        query = """
        query ($workspace_ids: [ID!]) {
            boards(limit: 500, workspace_ids: $workspace_ids) {
                id
                name
                workspace {
                    id
                }
            }
        }
        """
        data = self._execute_query(query, {"workspace_ids": [int(workspace_id)]})
        return [board for board in data.get("boards", []) if isinstance(board, dict)]

    def find_board(self, workspace_id: str, names: Sequence[str]) -> Dict[str, Any]:
        """
        Find a board by exact normalized name first, then partial normalized name.

        Raises RuntimeError when no matching board is found, matching the
        previous TimeBuzzer MondayClient behavior.
        """
        wanted = {_normalize_name(name) for name in names}
        boards = self.list_boards(workspace_id)
        for board in boards:
            if _normalize_name(board.get("name")) in wanted:
                return board
        for board in boards:
            board_name = _normalize_name(board.get("name"))
            if any(name in board_name for name in wanted):
                return board
        names_display = ", ".join(names)
        raise RuntimeError(f"Could not find board named {names_display!r} in workspace {workspace_id}.")

    def get_board_items(
        self,
        board_id: str,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Fetch board metadata and all items, including Monday pagination.

        This returns the shape expected by TimeBuzzer sync:
        (board, items), where board includes columns and items include subitems.
        """
        fields = """
            id
            name
            state
            created_at
            updated_at
            url
            group {
                id
                title
                color
            }
            column_values {
                id
                text
                value
                type
                ... on BoardRelationValue {
                    linked_item_ids
                    display_value
                }
                ... on MirrorValue {
                    display_value
                }
            }
            subitems {
                id
                name
                state
                created_at
                updated_at
                url
                column_values {
                    id
                    text
                    value
                    type
                    ... on MirrorValue {
                        display_value
                    }
                }
            }
        """
        query = f"""
        query ($board_id: [ID!], $limit: Int!) {{
            boards(ids: $board_id) {{
                id
                name
                description
                columns {{
                    id
                    title
                    type
                }}
                items_page(limit: $limit) {{
                    cursor
                    items {{
                        {fields}
                    }}
                }}
            }}
        }}
        """
        data = self._execute_query(query, {"board_id": [int(board_id)], "limit": int(limit)})
        boards = data.get("boards") or []
        if not boards:
            raise RuntimeError(f"Monday board {board_id} was not found.")

        board = boards[0]
        items_page = board.get("items_page") or {}
        items = list(items_page.get("items") or [])
        cursor = items_page.get("cursor")

        while cursor:
            page_query = f"""
            query ($cursor: String!, $limit: Int!) {{
                next_items_page(cursor: $cursor, limit: $limit) {{
                    cursor
                    items {{
                        {fields}
                    }}
                }}
            }}
            """
            page_data = self._execute_query(page_query, {"cursor": cursor, "limit": int(limit)})
            page = page_data.get("next_items_page") or {}
            items.extend(page.get("items") or [])
            cursor = page.get("cursor")

        return board, items
    
    def get_folder_id_by_name(self, workspace_id: str, folder_name: str) -> Optional[str]:
        """
        Get folder ID by folder name in a workspace.
        
        Args:
            workspace_id: Monday.com workspace ID
            folder_name: Name of the folder (e.g., "Weekly Reports")
            
        Returns:
            Folder ID or None if not found
        """
        query = """
        query ($workspace_ids: [ID!]) {
            folders(workspace_ids: $workspace_ids) {
                id
                name
            }
        }
        """
        
        variables = {"workspace_ids": [int(workspace_id)]}
        
        try:
            data = self._execute_query(query, variables)
            folders = data.get('folders', [])
            for folder in folders:
                if folder['name'] == folder_name:
                    logger.info(f"Found folder '{folder_name}' with ID: {folder['id']}")
                    return folder['id']
            logger.warning(f"Folder '{folder_name}' not found in workspace {workspace_id}")
            return None
        except Exception as e:
            logger.error(f"Error fetching folders: {str(e)}")
            return None

    def get_group_id_by_name(self, board_id: str, group_name: str) -> Optional[str]:
        """
        Get group ID by group name on a board.

        Args:
            board_id: Monday.com board ID
            group_name: Name of the group (e.g., "Sprint Report")

        Returns:
            Group ID or None if not found
        """
        query = """
        query ($board_id: [ID!]) {
            boards(ids: $board_id) {
                groups {
                    id
                    title
                }
            }
        }
        """

        variables = {"board_id": [int(board_id)]}

        try:
            data = self._execute_query(query, variables)
            groups = data.get('boards', [{}])[0].get('groups', [])
            for group in groups:
                if group['title'] == group_name:
                    return group['id']
            return None
        except Exception as e:
            logger.error(f"Error fetching group: {str(e)}")
            return None

    def get_item_by_name(self, board_id: str, item_name: str, group_id: Optional[str] = None) -> Optional[Dict]:
        """
        Get an item by its name from a board.

        Args:
            board_id: Monday.com board ID
            item_name: Name of the item to find
            group_id: Optional group ID to search within

        Returns:
            Item object with id, name, and other details, or None if not found
        """
        query = """
        query ($board_id: [ID!]) {
            boards(ids: $board_id) {
                items_page(limit: 500) {
                    items {
                        id
                        name
                        group {
                            id
                            title
                        }
                    }
                }
            }
        }
        """

        variables = {"board_id": [int(board_id)]}

        try:
            data = self._execute_query(query, variables)
            items = data.get('boards', [{}])[0].get('items_page', {}).get('items', [])

            for item in items:
                if item.get('name') == item_name:
                    if group_id:
                        item_group = item.get('group', {})
                        if item_group and item_group.get('id') == group_id:
                            return item
                    else:
                        return item

            return None
        except Exception as e:
            logger.error(f"Error finding item by name: {str(e)}")
            return None

    def create_update_on_item(self, item_id: str, update_body: str) -> bool:
        """
        Create an update (comment) on an item.

        Args:
            item_id: Monday.com item ID
            update_body: The text content of the update

        Returns:
            True if successful, False otherwise
        """
        mutation = """
        mutation ($item_id: ID!, $body: String!) {
            create_update(
                item_id: $item_id
                body: $body
            ) {
                id
            }
        }
        """

        variables = {
            "item_id": str(item_id),
            "body": update_body
        }

        try:
            self._execute_query(mutation, variables)
            logger.info(f"Created update on item {item_id}")
            return True
        except Exception as e:
            logger.error(f"Error creating update on item: {str(e)}")
            return False

    def get_item_updates(self, item_id: str, limit: int = 100) -> List[Dict]:
        """
        Get updates/comments for an item (works for items and subitems).

        Args:
            item_id: Monday.com item/subitem ID
            limit: Maximum number of updates to fetch

        Returns:
            List of update dicts (id, body, created_at)
        """
        query = """
        query ($item_ids: [ID!], $limit: Int!) {
            items(ids: $item_ids) {
                id
                updates(limit: $limit) {
                    id
                    body
                    created_at
                }
            }
        }
        """

        variables = {
            "item_ids": [str(item_id)],
            "limit": int(limit)
        }

        try:
            data = self._execute_query(query, variables)
            items = data.get('items', [])
            if not items:
                return []
            return items[0].get('updates', []) or []
        except Exception as e:
            logger.error(f"Error getting updates for item {item_id}: {str(e)}")
            return []

    def copy_missing_updates(self, source_item_id: str, target_item_id: str, limit: int = 100) -> Dict:
        """
        Copy updates from source item to target item while avoiding duplicates.

        Duplicate detection is body-based with occurrence counting, so repeated
        updates with the same body are preserved.

        Args:
            source_item_id: Source item/subitem ID
            target_item_id: Target item/subitem ID
            limit: Maximum number of updates to inspect/copy

        Returns:
            Summary dict with source_count, target_count, copied, skipped, failed
        """
        source_updates = self.get_item_updates(source_item_id, limit=limit)
        target_updates = self.get_item_updates(target_item_id, limit=limit)

        target_body_counts = {}
        for update in target_updates:
            body = (update.get('body') or '').strip()
            if body:
                target_body_counts[body] = target_body_counts.get(body, 0) + 1

        copied = 0
        skipped = 0
        failed = 0

        # Monday returns newest-first; copy oldest-first to preserve chronology.
        for update in reversed(source_updates):
            body = (update.get('body') or '').strip()
            if not body:
                skipped += 1
                continue

            if target_body_counts.get(body, 0) > 0:
                target_body_counts[body] -= 1
                skipped += 1
                continue

            if self.create_update_on_item(target_item_id, body):
                copied += 1
            else:
                failed += 1

        return {
            'source_count': len(source_updates),
            'target_count': len(target_updates),
            'copied': copied,
            'skipped': skipped,
            'failed': failed,
        }

    def create_item_on_board(self, board_id: str, group_id: str, item_name: str,
                             column_values: Optional[Dict] = None) -> Optional[str]:
        """
        Create a new item on a Monday board in a specific group.

        Args:
            board_id: Monday.com board ID
            group_id: Group ID (e.g., "Sprint Report")
            item_name: Name of the item to create
            column_values: Dictionary of column_id -> value mappings

        Returns:
            Created item ID or None if failed
        """
        formatted_columns = []
        if column_values:
            for col_id, value in column_values.items():
                formatted_columns.append(f'{col_id}: {json.dumps({"value": value})}')

        columns_str = ', '.join(formatted_columns) if formatted_columns else ''

        if columns_str:
            mutation = f"""
            mutation {{
                create_item(
                    board_id: {board_id}
                    group_id: "{group_id}"
                    item_name: "{item_name}"
                    column_values: {{{columns_str}}}
                ) {{
                    id
                }}
            }}
            """
        else:
            mutation = f"""
            mutation {{
                create_item(
                    board_id: {board_id}
                    group_id: "{group_id}"
                    item_name: "{item_name}"
                ) {{
                    id
                }}
            }}
            """

        try:
            data = self._execute_query(mutation)
            item_id = data.get('create_item', {}).get('id')
            logger.info(f"Created item {item_id} on board {board_id}")
            return item_id
        except Exception as e:
            logger.error(f"Error creating item: {str(e)}")
            return None
    
    def get_boards_in_folder(self, workspace_id: str, folder_id: str) -> List[Dict]:
        """
        Get all boards within a specific folder.
        
        Args:
            workspace_id: Monday.com workspace ID
            folder_id: Monday.com folder ID
            
        Returns:
            List of board objects with id, name, and groups
        """
        # First, get all boards in the workspace
        query = """
        query ($workspace_ids: [ID!]) {
            boards(limit: 500, workspace_ids: $workspace_ids) {
                id
                name
                groups {
                    id
                    title
                }
                workspace {
                    id
                }
            }
        }
        """
        
        variables = {"workspace_ids": [int(workspace_id)]}
        
        try:
            data = self._execute_query(query, variables)
            all_boards = data.get('boards', [])
            
            # Now query the folder to get its children IDs
            folder_query = """
            query ($workspace_ids: [ID!]) {
                folders(workspace_ids: $workspace_ids) {
                    id
                    name
                    children {
                        id
                    }
                }
            }
            """
            
            folder_data = self._execute_query(folder_query, variables)
            folders = folder_data.get('folders', [])
            
            # Find our specific folder and get child IDs
            child_ids = set()
            for folder in folders:
                if str(folder['id']) == str(folder_id):
                    for child in folder.get('children', []):
                        child_ids.add(str(child['id']))
                    break
            
            if not child_ids:
                logger.warning(f"No children found in folder {folder_id}")
                return []
            
            # Filter boards that are in the folder
            folder_boards = []
            for board in all_boards:
                if str(board['id']) in child_ids:
                    folder_boards.append(board)
            
            logger.info(f"Found {len(folder_boards)} boards in folder {folder_id}")
            return folder_boards
        except Exception as e:
            logger.error(f"Error fetching boards in folder: {str(e)}")
            return []
    
    def create_doc_in_folder_with_table(self, workspace_id: str, folder_id: str, title: str, content: str, tables: List[Dict]) -> Optional[str]:
        """
        Create a Monday.com doc in a specific folder with multiple tables.
        
        Args:
            workspace_id: Monday.com workspace ID
            folder_id: Monday.com folder ID
            title: Title of the document
            content: Text content of the document
            tables: List of table dictionaries, each with 'title' and 'rows' keys
                   Example: [{"title": "Task Details", "rows": [...]}, {"title": "Hours Summary", "rows": [...]}]
            
        Returns:
            Doc ID or None if failed
        """
        logger.info(f"=== START create_doc_in_folder_with_table ===")
        
        # STEP 1: Validate input parameters
        logger.info("STEP 1: Validating input parameters")
        try:
            if not workspace_id or not folder_id or not title:
                logger.error(f"STEP 1 FAILED: Missing required parameters - workspace_id={bool(workspace_id)}, folder_id={bool(folder_id)}, title={bool(title)}")
                return None
            
            workspace_id_int = int(workspace_id)
            folder_id_int = int(folder_id)
            content_len = len(content) if content else 0
            
            # Validate tables structure
            tables_count = 0
            total_rows = 0
            if tables and isinstance(tables, list):
                tables_count = len(tables)
                for table in tables:
                    if isinstance(table, dict) and 'rows' in table:
                        total_rows += len(table.get('rows', []))
            
            logger.info(f"  ✓ workspace_id: {workspace_id_int}")
            logger.info(f"  ✓ folder_id: {folder_id_int}")
            logger.info(f"  ✓ title: '{title}' ({len(title)} chars)")
            logger.info(f"  ✓ content: {content_len} chars")
            logger.info(f"  ✓ tables: {tables_count} tables, {total_rows} total rows")
            
            if tables_count > 0:
                for idx, table in enumerate(tables):
                    table_title = table.get('title', f'Table {idx+1}')
                    table_rows = table.get('rows', [])
                    table_cols = len(table_rows[0].get('cells', [])) if table_rows and len(table_rows) > 0 else 0
                    logger.info(f"    Table {idx+1}: '{table_title}' - {len(table_rows)} rows x {table_cols} cols")
        except Exception as e:
            logger.error(f"STEP 1 FAILED: Parameter validation error: {str(e)}")
            return None
        
        # STEP 2: Build and execute doc creation mutation
        logger.info("STEP 2: Building doc creation mutation")
        create_mutation = """
        mutation ($workspace_id: ID!, $folder_id: ID!, $title: String!) {
            create_doc(
                location: {
                    workspace: {
                        workspace_id: $workspace_id
                        folder_id: $folder_id
                        name: $title
                    }
                }
            ) {
                id
            }
        }
        """
        
        variables = {
            "workspace_id": workspace_id_int,
            "folder_id": folder_id_int,
            "title": title
        }
        
        logger.info(f"  Variables: workspace_id={variables['workspace_id']}, folder_id={variables['folder_id']}, title={variables['title']}")
        
        try:
            logger.info("STEP 3: Executing doc creation query")
            data = self._execute_query(create_mutation, variables)
            logger.info(f"  Query executed successfully. Response keys: {list(data.keys())}")
            
            doc_id = data.get('create_doc', {}).get('id')
            
            if not doc_id:
                logger.error(f"STEP 3 FAILED: No doc ID in response. Full response: {data}")
                return None
            
            logger.info(f"  ✓ Doc created successfully with ID: {doc_id}")
                
        except Exception as e:
            logger.error(f"STEP 3 FAILED: Doc creation query error: {str(e)}")
            logger.error(f"  Exception type: {type(e).__name__}")
            return None
        
        # STEP 4: Add title block
        logger.info("STEP 4: Adding title block to doc")
        try:
            title_mutation = """
            mutation ($doc_id: ID!, $content: JSON!) {
                create_doc_block(
                    doc_id: $doc_id
                    type: normal_text
                    content: $content
                ) {
                    id
                }
            }
            """
            
            title_variables = {
                "doc_id": doc_id,
                "content": json.dumps({
                    "deltaFormat": [{"insert": title + "\n\n"}]
                })
            }
            logger.info(f"  Executing title block mutation for doc_id={doc_id}, content_length={len(title_variables['content'])}")
            self._execute_query(title_mutation, title_variables)
            logger.info(f"  ✓ Title block added successfully")
        except Exception as title_error:
            logger.error(f"STEP 4 FAILED: Could not add title block: {str(title_error)}")
            logger.error(f"  Exception type: {type(title_error).__name__}")
            # Don't return yet - try to continue with content
        
        # STEP 5: Add text content block
        logger.info("STEP 5: Adding text content block to doc")
        try:
            content_variables = {
                "doc_id": doc_id,
                "content": json.dumps({
                    "deltaFormat": [{"insert": content + "\n\n"}]
                })
            }
            logger.info(f"  Executing content block mutation for doc_id={doc_id}, content_length={len(content_variables['content'])}")
            self._execute_query(title_mutation, content_variables)
            logger.info(f"  ✓ Content block added successfully")
        except Exception as content_error:
            logger.error(f"STEP 5 FAILED: Could not add content block: {str(content_error)}")
            logger.error(f"  Exception type: {type(content_error).__name__}")
            # Don't return yet - try to continue with tables
        
        # STEP 6: Add tables (if tables provided)
        logger.info(f"STEP 6: Processing tables ({tables_count} tables)")
        if tables and isinstance(tables, list) and len(tables) > 0:
            for table_idx, table in enumerate(tables):
                try:
                    table_title = table.get('title', f'Table {table_idx+1}')
                    table_rows = table.get('rows', [])
                    
                    logger.info(f"  STEP 6.{table_idx+1}: Creating table '{table_title}' ({len(table_rows)} rows)")
                    
                    if table_rows and len(table_rows) > 0:
                        # Add separator/title for this table
                        separator_text = f"\n\n📊 {table_title}\n{'='*60}\n"
                        separator_mutation = """
                        mutation ($doc_id: ID!, $content: JSON!) {
                            create_doc_block(
                                doc_id: $doc_id
                                type: normal_text
                                content: $content
                            ) {
                                id
                            }
                        }
                        """
                        separator_variables = {
                            "doc_id": doc_id,
                            "content": json.dumps({"deltaFormat": [{"insert": separator_text}]})
                        }
                        separator_response = self._execute_query(separator_mutation, separator_variables)
                        separator_id = separator_response.get('create_doc_block', {}).get('id')
                        logger.info(f"    ✓ Table title separator added")
                        
                        # Create the table block after the separator so title stays above
                        self._create_table_block(doc_id, table_rows, after_block_id=separator_id)
                        logger.info(f"    ✓ Table '{table_title}' created successfully")
                    else:
                        logger.warning(f"    No rows found for table '{table_title}', skipping")
                        
                except Exception as table_error:
                    logger.error(f"  STEP 6.{table_idx+1} FAILED: Could not add table '{table_title}': {str(table_error)}")
                    logger.error(f"    Exception type: {type(table_error).__name__}")
                    # Continue to next table even if this one fails
        else:
            logger.info(f"  No tables provided, skipping table creation")
        
        # STEP 7: Final success confirmation
        logger.info(f"STEP 7: Finalizing - returning doc_id={doc_id}")
        logger.info(f"=== SUCCESS: create_doc_in_folder_with_table completed for doc_id={doc_id} ===")
        return doc_id
    
    
    def _create_table_block(self, doc_id: str, table_rows: List[Dict], after_block_id: Optional[str] = None) -> None:
        """Create a proper table block in the Monday.com doc using the table block API.
        Based on: https://community.monday.com/t/how-to-use-the-api-to-create-a-table-in-a-workdoc/100228
        """
        logger.info(f"=== Starting _create_table_block for doc_id: {doc_id} ===")
        
        if not table_rows or len(table_rows) == 0:
            logger.warning("No table rows provided, returning early")
            return
        
        num_rows = len(table_rows)
        num_cols = len(table_rows[0]["cells"]) if table_rows else 0
        
        logger.info(f"Table dimensions: {num_rows} rows x {num_cols} columns")
        logger.debug(f"First row sample: {table_rows[0]['cells'][:3] if table_rows[0]['cells'] else 'empty'}")
        
        if num_cols == 0:
            logger.warning("No columns detected, returning early")
            return
        
        try:
            # Step 1: Create the table block structure using create_doc_block (not create_doc_blocks)
            logger.info("Step 1: Creating table block structure with create_doc_block")
            table_mutation = """
            mutation ($doc_id: ID!, $type: DocBlockContentType!, $content: JSON!, $after_block_id: String) {
                create_doc_block(
                    doc_id: $doc_id
                    type: $type
                    content: $content
                    after_block_id: $after_block_id
                ) {
                    id
                    content
                }
            }
            """
            
            # Create table block content as JSON string
            # Note: columnsStyle is auto-generated by Monday.com and cannot be set during creation
            table_content = json.dumps({
                "column_count": num_cols,
                "row_count": num_rows
            })
            
            variables = {
                "doc_id": doc_id,
                "type": "table",
                "content": table_content,
                "after_block_id": after_block_id
            }
            
            logger.info(f"Sending table mutation with variables: {json.dumps(variables, indent=2)}")
            table_response = self._execute_query(table_mutation, variables)
            
            if not table_response or 'create_doc_block' not in table_response:
                logger.error("Failed to create table block - no 'create_doc_block' in response")
                logger.error(f"Response was: {table_response}")
                return
            
            # Step 2: Extract cell block IDs from the response content
            logger.info("Step 2: Extracting cell block IDs from response")
            table_block = table_response['create_doc_block']
            logger.debug(f"table_block: {json.dumps(table_block, indent=2)}")
            
            # The content field contains a JSON string with cell IDs
            content_str = table_block.get('content')
            if not content_str:
                logger.error("No content field in table block response")
                return
            
            try:
                content_data = json.loads(content_str)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse content JSON: {e}")
                logger.error(f"Content was: {content_str}")
                return
            
            # Extract cells array from content
            # The structure should be: {"cells": [[{"id": "cell-id-1"}, {"id": "cell-id-2"}], [...]]}
            cells_data = content_data.get('cells', [])
            if not cells_data:
                logger.warning("No cells array found in content")
                logger.warning(f"Content structure: {content_data}")
                return
            
            logger.info(f"Found {len(cells_data)} rows in cells array")
            
            
            # Step 3: Populate each cell with content using parent_block_id
            logger.info("Step 3: Adding content to each cell")
            cell_mutation = """
            mutation ($doc_id: ID!, $parent_block_id: String, $type: DocBlockContentType!, $content: JSON!) {
                create_doc_block(
                    doc_id: $doc_id
                    parent_block_id: $parent_block_id
                    type: $type
                    content: $content
                ) {
                    id
                }
            }
            """
            
            # Iterate through cells and add content
            cells_created = 0
            for row_idx, row_cells in enumerate(cells_data):
                if row_idx >= len(table_rows):
                    logger.warning(f"Row {row_idx} exceeds table_rows length, skipping")
                    continue
                
                logger.debug(f"Processing row {row_idx} with {len(row_cells)} cells")
                
                for col_idx, cell_info in enumerate(row_cells):
                    if col_idx >= len(table_rows[row_idx]["cells"]):
                        logger.warning(f"Cell [{row_idx}][{col_idx}] exceeds table_rows columns, skipping")
                        continue
                    
                    # Get the cell ID (Monday.com uses 'blockId' instead of 'id')
                    cell_id = cell_info.get('blockId')
                    if not cell_id:
                        logger.warning(f"Cell [{row_idx}][{col_idx}] has no blockId, skipping")
                        continue
                    
                    # Get the content for this cell
                    cell_content = str(table_rows[row_idx]["cells"][col_idx].get("insert", ""))
                    
                    # Add dash prefix for multi-line content (tasks) in non-header rows
                    if row_idx > 0 and '\n' in cell_content:
                        # Split by newlines, add dash to non-empty lines, rejoin
                        lines = cell_content.split('\n')
                        formatted_lines = ['- ' + line if line.strip() else line for line in lines]
                        cell_content = '\n'.join(formatted_lines)
                    
                    logger.debug(f"  Cell [{row_idx}][{col_idx}]: id={cell_id}, content='{cell_content[:50]}'")
                    
                    # Create content block for this cell
                    cell_content_json = json.dumps({
                        "alignment": "left",
                        "direction": "ltr",
                        "deltaFormat": [{
                            "insert": cell_content,
                            "attributes": {
                                "bold": row_idx == 0  # Make header row bold
                            }
                        }]
                    })
                    
                    cell_variables = {
                        "doc_id": doc_id,
                        "parent_block_id": cell_id,
                        "type": "normal_text",
                        "content": cell_content_json
                    }
                    
                    try:
                        cell_response = self._execute_query(cell_mutation, cell_variables)
                        cells_created += 1
                        logger.debug(f"    Cell [{row_idx}][{col_idx}] content added successfully")
                    except Exception as cell_error:
                        logger.error(f"    Failed to add content to cell [{row_idx}][{col_idx}]: {cell_error}")
            
            logger.info(f"✅ Successfully created table with {num_rows} rows x {num_cols} columns ({cells_created} cells populated)")
            logger.info(f"=== Completed _create_table_block for doc_id: {doc_id} ===")
            
        except Exception as e:
            logger.error(f"❌ Error creating table block: {str(e)}")
            logger.error(f"Exception type: {type(e).__name__}")
            logger.error(f"Exception details: {e}", exc_info=True)
            
            # Fall back to text format if table creation fails
            logger.info("Attempting fallback to text format table...")
            try:
                table_text = self._format_table_as_text(table_rows)
                logger.debug(f"Generated fallback text table ({len(table_text)} chars)")
                
                fallback_mutation = """
                mutation ($doc_id: ID!, $content: JSON!) {
                    create_doc_block(
                        doc_id: $doc_id
                        type: normal_text
                        content: $content
                    ) {
                        id
                    }
                }
                """
                fallback_variables = {
                    "doc_id": doc_id,
                    "content": json.dumps({
                        "deltaFormat": [{"insert": table_text}]
                    })
                }
                
                logger.info("Sending fallback text block mutation")
                self._execute_query(fallback_mutation, fallback_variables)
                logger.info("✅ Fallback text table created successfully")
            except Exception as fallback_error:
                logger.error(f"❌ Fallback also failed: {str(fallback_error)}")
                logger.error(f"Fallback exception details: {fallback_error}", exc_info=True)
    
    def _format_table_as_text(self, table_rows: List[Dict]) -> str:
        """Convert table rows to formatted text representation (fallback method)."""
        if not table_rows:
            return ""
        
        # Calculate column widths
        col_widths = []
        for col_idx in range(len(table_rows[0]["cells"])):
            max_width = max(len(str(row["cells"][col_idx]["insert"]).split('\n')[0]) for row in table_rows)
            col_widths.append(max(max_width + 2, 15))  # Min width of 15
        
        # Build the text table
        table_text = "\n\n" + "=" * 140 + "\n"
        table_text += "📊 SPRINT REPORT SUMMARY TABLE\n"
        table_text += "=" * 140 + "\n\n"
        
        for row_idx, row in enumerate(table_rows):
            # Add separator line before header and summary
            if row_idx == 0 or row_idx == len(table_rows) - 1:
                table_text += "-" * 140 + "\n"
            
            # Add row content
            row_parts = []
            for col_idx, cell in enumerate(row["cells"]):
                cell_text = str(cell["insert"])
                # For multi-line cells (tasks), show count + first task
                if '\n' in cell_text and row_idx > 0 and row_idx < len(table_rows) - 1:
                    lines = [line for line in cell_text.split('\n') if line.strip()]
                    if len(lines) > 1:
                        cell_text = f"({len(lines)}) {lines[0][:25]}..."
                    elif len(lines) == 1:
                        cell_text = lines[0][:30]
                    else:
                        cell_text = ""
                else:
                    cell_text = cell_text[:30]
                row_parts.append(cell_text.ljust(col_widths[col_idx] if col_idx < len(col_widths) else 15))
            
            table_text += " | ".join(row_parts) + "\n"
            
            # Add separator after header
            if row_idx == 0:
                table_text += "-" * 140 + "\n"
        
        table_text += "=" * 140 + "\n\n"
        
        return table_text
    
    def _extract_product_from_columns(self, columns_by_name: Dict) -> str:
        """
        Extract product/project name from columns using various patterns.
        Checks lookup/mirror columns that might contain product information.
        """
        # Try common product-related column name patterns
        product_patterns = [
            'Product', 'Project', 'Produkt', 'Projekt', 'Products',
            'Product Name', 'Project Name',
            'Related Product', 'Related Project'
        ]
        
        for pattern in product_patterns:
            if pattern in columns_by_name:
                col = columns_by_name[pattern]
                value = col.get('display_value', '') or col.get('text', '')
                if value:
                    return value
        
        # Try any column with 'product' or 'project' in the name (case insensitive)
        for col_name, col_data in columns_by_name.items():
            if 'product' in col_name.lower() or 'project' in col_name.lower():
                if col_data.get('type') in ['mirror', 'lookup', 'board_relation', 'text', 'status', 'color']:
                    value = col_data.get('display_value', '') or col_data.get('text', '')
                    if value:
                        return value
        
        return ''
        
    def get_sprints_board(self, workspace_id: Optional[str] = None) -> Optional[Dict]:
        """
        Find the Sprints board in the workspace.
        
        Args:
            workspace_id: Optional specific workspace ID to search in
        
        Returns:
            Sprints board info with id and name
        """
        # Determine which workspace IDs to use
        target_workspace_ids = [workspace_id] if workspace_id else self.workspace_ids
        
        if not target_workspace_ids:
            # No workspace filter, search all boards (not recommended)
            query = """
            query {
                boards(limit: 500) {
                    id
                    name
                    workspace {
                        id
                    }
                }
            }
            """
            variables = None
        else:
            # Filter by workspace IDs in the query for better performance
            query = """
            query ($workspace_ids: [ID!]) {
                boards(limit: 500, workspace_ids: $workspace_ids) {
                    id
                    name
                    workspace {
                        id
                    }
                }
            }
            """
            variables = {"workspace_ids": [int(wid) for wid in target_workspace_ids]}
        
        try:
            data = self._execute_query(query, variables)
            for board in data.get('boards', []):
                board_workspace_id = board.get('workspace', {}).get('id')
                
                if board['name'].lower() == "sprints":
                    logger.info(f"Found Sprints board: {board['name']} (ID: {board['id']}, Workspace: {board_workspace_id})")
                    return {'id': board['id'], 'name': board['name'], 'workspace_id': board_workspace_id}
            return None
        except Exception as e:
            logger.error(f"Error fetching sprints board: {str(e)}")
            return None
    
    def get_active_sprint(self, sprints_board_id: str) -> Optional[Dict]:
        """
        Get the sprint marked as 'Active Sprint' = true.
        
        Args:
            sprints_board_id: ID of the Sprints board
            
        Returns:
            Active sprint object with id, name, and column data
        """
        query = """
        query ($board_id: [ID!]) {
            boards(ids: $board_id) {
                items_page(limit: 100) {
                    items {
                        id
                        name
                        column_values {
                            id
                            text
                            value
                        }
                    }
                }
            }
        }
        """
        
        variables = {"board_id": [int(sprints_board_id)]}
        
        try:
            data = self._execute_query(query, variables)
            items = data.get('boards', [{}])[0].get('items_page', {}).get('items', [])
            print(f"Total sprints found: {len(items)}")
            
            active_sprints = []

            for item in items:
                columns = {cv['id']: cv for cv in item.get('column_values', [])}
                
                is_active = False

                # Check for sprint_activation column specifically
                if 'sprint_activation' in columns:
                    col_data = columns['sprint_activation']
                    value = col_data.get('value', '')
                    print(f"  sprint_activation value: {value}")
                    
                    # Parse the JSON value and check if checked is true
                    if value and '"checked":true' in value:
                        is_active = True
                
                # Fallback: check for any column with 'active' or 'current' in name
                if not is_active:
                    for col_id, col_data in columns.items():
                        if 'active' in col_id.lower() or 'current' in col_id.lower():
                            value = col_data.get('value', '')
                            text = col_data.get('text', '')
                            if '"checked":true' in value or text.lower() == 'true' or 'true' in str(value).lower():
                                is_active = True
                                break

                if is_active:
                    active_sprints.append({
                        'id': item['id'],
                        'name': item['name'],
                        'columns': columns
                    })

            if not active_sprints:
                logger.warning("No active sprint found")
                return None

            if len(active_sprints) == 1:
                logger.info(f"Found active sprint: {active_sprints[0]['name']}")
                return active_sprints[0]

            # Multiple active sprints — return the one with the highest sprint number
            def extract_sprint_number(sprint):
                import re
                match = re.search(r'(\d+)', sprint['name'])
                return int(match.group(1)) if match else -1

            latest_sprint = max(active_sprints, key=extract_sprint_number)
            logger.info(
                f"Multiple active sprints found: {[s['name'] for s in active_sprints]}. "
                f"Selecting latest: {latest_sprint['name']}"
            )
            return latest_sprint

        except Exception as e:
            logger.error(f"Error getting active sprint: {str(e)}")
            return None
    
    def get_next_sprint(self, sprints_board_id: str) -> Optional[Dict]:
        """
        Get the next/planned sprint (usually the last sprint in the list that's not active).
        
        Args:
            sprints_board_id: ID of the Sprints board
            
        Returns:
            Next sprint object with id, name, and column data
        """
        query = """
        query ($board_id: [ID!]) {
            boards(ids: $board_id) {
                items_page(limit: 100) {
                    items {
                        id
                        name
                        column_values {
                            id
                            text
                            value
                        }
                    }
                }
            }
        }
        """
        
        variables = {"board_id": [int(sprints_board_id)]}
        
        try:
            data = self._execute_query(query, variables)
            items = data.get('boards', [{}])[0].get('items_page', {}).get('items', [])
            
            # Find the first sprint that is NOT active
            for item in items:
                columns = {cv['id']: cv for cv in item.get('column_values', [])}
                
                is_active = False
                
                # Check sprint_activation column specifically
                if 'sprint_activation' in columns:
                    col_data = columns['sprint_activation']
                    value = col_data.get('value', '')
                    if value and '"checked":true' in value:
                        is_active = True
                
                # Fallback: check for any column with 'active' or 'current' in name
                if not is_active:
                    for col_id, col_data in columns.items():
                        if 'active' in col_id.lower() or 'current' in col_id.lower():
                            value = col_data.get('value', '')
                            text = col_data.get('text', '')
                            if '"checked":true' in value or text.lower() == 'true' or 'true' in str(value).lower():
                                is_active = True
                                break
                
                # Return the first non-active sprint
                if not is_active:
                    logger.info(f"Found next sprint: {item['name']}")
                    return {
                        'id': item['id'],
                        'name': item['name'],
                        'columns': columns
                    }
            
            logger.warning("No next sprint found")
            return None
        except Exception as e:
            logger.error(f"Error getting next sprint: {str(e)}")
            return None

    def get_latest_sprint(self, sprints_board_id: str) -> Optional[Dict]:
        """
        Get the latest (newly created) sprint by finding the sprint with the highest
        sprint number that is NOT the currently active sprint.

        This is used for 'In Transfer' logic where a task needs to be moved to
        the newest planned sprint.

        Args:
            sprints_board_id: ID of the Sprints board

        Returns:
            Sprint object with id, name, and columns, or None if not found
        """
        import re as _re

        query = """
        query ($board_id: [ID!]) {
            boards(ids: $board_id) {
                items_page(limit: 100) {
                    items {
                        id
                        name
                        column_values {
                            id
                            text
                            value
                        }
                    }
                }
            }
        }
        """
        variables = {"board_id": [int(sprints_board_id)]}

        try:
            data = self._execute_query(query, variables)
            items = data.get('boards', [{}])[0].get('items_page', {}).get('items', [])

            def _is_sprint_active(item):
                columns = {cv['id']: cv for cv in item.get('column_values', [])}
                if 'sprint_activation' in columns:
                    val = columns['sprint_activation'].get('value', '')
                    if val and '"checked":true' in val:
                        return True
                for col_id, col_data in columns.items():
                    if 'active' in col_id.lower() or 'current' in col_id.lower():
                        val = col_data.get('value', '')
                        txt = col_data.get('text', '')
                        if '"checked":true' in val or txt.lower() == 'true':
                            return True
                return False

            def _sprint_number(name: str) -> int:
                match = _re.search(r'(\d+)', name)
                return int(match.group(1)) if match else -1

            # Collect all non-active sprints and find the one with the highest number
            candidates = []
            for item in items:
                if not _is_sprint_active(item):
                    num = _sprint_number(item['name'])
                    columns = {cv['id']: cv for cv in item.get('column_values', [])}
                    candidates.append({'id': item['id'], 'name': item['name'],
                                       'columns': columns, '_num': num})

            if not candidates:
                # Fallback: if every sprint is active (edge case), just return the
                # one with the highest number so we still have something to link to
                for item in items:
                    num = _sprint_number(item['name'])
                    columns = {cv['id']: cv for cv in item.get('column_values', [])}
                    candidates.append({'id': item['id'], 'name': item['name'],
                                       'columns': columns, '_num': num})

            if not candidates:
                logger.warning("No sprints found in Sprints board")
                return None

            latest = max(candidates, key=lambda s: s['_num'])
            logger.info(f"Latest sprint (highest number, non-active): {latest['name']} (ID: {latest['id']})")
            return {'id': latest['id'], 'name': latest['name'], 'columns': latest['columns']}

        except Exception as e:
            logger.error(f"Error getting latest sprint: {str(e)}")
            return None

    def duplicate_item(self, item_id: str, board_id: str, with_updates: bool = False) -> Optional[str]:
        """
        Duplicate an item on a Monday.com board.

        Args:
            item_id: ID of the item to duplicate
            board_id: Board ID where the item lives
            with_updates: Whether to also copy updates/comments

        Returns:
            The new (duplicate) item ID, or None on failure
        """
        mutation = """
        mutation ($boardId: ID!, $itemId: ID!, $withUpdates: Boolean) {
            duplicate_item(
                board_id: $boardId
                item_id: $itemId
                with_updates: $withUpdates
            ) {
                id
            }
        }
        """
        variables = {
            "boardId": str(board_id),
            "itemId": str(item_id),
            "withUpdates": with_updates,
        }

        try:
            data = self._execute_query(mutation, variables)
            new_id = data.get('duplicate_item', {}).get('id')
            if new_id:
                logger.info(f"Duplicated item {item_id} -> new item {new_id} on board {board_id}")
            else:
                logger.error(f"duplicate_item returned no ID. Response: {data}")
            return new_id
        except Exception as e:
            logger.error(f"Error duplicating item {item_id}: {str(e)}")
            return None

    def get_previous_sprint(self, sprints_board_id: str, active_sprint_id: str) -> Optional[Dict]:
        """
        Get the previous sprint (the sprint before the active one).
        
        Args:
            sprints_board_id: ID of the Sprints board
            active_sprint_id: ID of the active sprint to find the previous one
            
        Returns:
            Previous sprint object with id, name, and column data
        """
        query = """
        query ($board_id: [ID!]) {
            boards(ids: $board_id) {
                items_page(limit: 100) {
                    items {
                        id
                        name
                        column_values {
                            id
                            text
                            value
                        }
                    }
                }
            }
        }
        """
        
        variables = {"board_id": [int(sprints_board_id)]}
        
        try:
            data = self._execute_query(query, variables)
            items = data.get('boards', [{}])[0].get('items_page', {}).get('items', [])
            
            # Find the sprint immediately before the active sprint
            active_index = -1
            for i, item in enumerate(items):
                if item['id'] == active_sprint_id:
                    active_index = i
                    break
            
            # Get the sprint after the active one in the list (sprints are typically ordered newest to oldest)
            if active_index != -1 and active_index + 1 < len(items):
                prev_item = items[active_index + 1]
                columns = {cv['id']: cv for cv in prev_item.get('column_values', [])}
                logger.info(f"Found previous sprint: {prev_item['name']}")
                return {
                    'id': prev_item['id'],
                    'name': prev_item['name'],
                    'columns': columns
                }
            
            logger.warning("No previous sprint found")
            return None
        except Exception as e:
            logger.error(f"Error getting previous sprint: {str(e)}")
            return None

    def get_previous_n_sprints(self, sprints_board_id: str, active_sprint_id: str, n: int = 3) -> List[Dict]:
        """
        Get the last N completed sprints (sprints before the active one).

        Args:
            sprints_board_id: ID of the Sprints board
            active_sprint_id: ID of the active sprint
            n: Number of previous sprints to return (default 3)

        Returns:
            List of sprint objects (ordered from most-recent to oldest), up to n items
        """
        query = """
        query ($board_id: [ID!]) {
            boards(ids: $board_id) {
                items_page(limit: 100) {
                    items {
                        id
                        name
                        column_values {
                            id
                            text
                            value
                        }
                    }
                }
            }
        }
        """

        variables = {"board_id": [int(sprints_board_id)]}

        try:
            data = self._execute_query(query, variables)
            items = data.get('boards', [{}])[0].get('items_page', {}).get('items', [])

            active_index = -1
            for i, item in enumerate(items):
                if item['id'] == active_sprint_id:
                    active_index = i
                    break

            if active_index == -1:
                logger.warning("Active sprint not found in board items")
                return []

            # Sprints are ordered newest → oldest; items after active_index are older sprints
            previous_sprints = []
            for offset in range(1, n + 1):
                idx = active_index + offset
                if idx >= len(items):
                    break
                item = items[idx]
                columns = {cv['id']: cv for cv in item.get('column_values', [])}
                previous_sprints.append({
                    'id': item['id'],
                    'name': item['name'],
                    'columns': columns
                })

            logger.info(f"Found {len(previous_sprints)} previous sprint(s): {[s['name'] for s in previous_sprints]}")
            return previous_sprints

        except Exception as e:
            logger.error(f"Error getting previous {n} sprints: {str(e)}")
            return []

    def get_all_client_boards(self, workspace_id: Optional[str] = None) -> List[Dict]:
        """
        Get all client/customer boards from the "Client Boards" folder.
        
        Args:
            workspace_id: Optional specific workspace ID to search in
        
        Returns:
            List of board objects with id, name, and groups
        """
        # Determine which workspace ID to use
        target_workspace_id = workspace_id if workspace_id else (self.workspace_ids[0] if self.workspace_ids else None)
        
        if not target_workspace_id:
            logger.error("No workspace ID provided for getting client boards")
            return []
        
        try:
            # Find the "Client Boards" folder
            folder_id = self.get_folder_id_by_name(target_workspace_id, "Client Boards")
            
            if not folder_id:
                logger.warning(f"'Client Boards' folder not found in workspace {target_workspace_id}")
                return []
            
            # Get all boards in the Client Boards folder
            client_boards = self.get_boards_in_folder(target_workspace_id, folder_id)
            
            logger.info(f"Found {len(client_boards)} client boards in 'Client Boards' folder")
            return client_boards
        except Exception as e:
            logger.error(f"Error fetching client boards: {str(e)}")
            return []
    
    def get_user_stories_by_sprint(self, board_id: str, sprint_id: str, status_filter: Optional[str] = None, save_to_json: bool = True, limit: int = 10) -> List[Dict]:
        """
        Get user stories and tasks for a specific sprint from any board with comprehensive details.
        
        Args:
            board_id: Board ID (could be User Stories board or any client board)
            sprint_id: Sprint item ID to filter by
            status_filter: Optional status filter (e.g., 'Done', 'In Progress')
            save_to_json: If True, saves top N records to JSON file
            limit: Number of records to save to JSON (default 10)
            
        Returns:
            List of user stories/tasks with comprehensive details
        """
        query = """
        query ($board_id: [ID!]) {
            boards(ids: $board_id) {
                id
                name
                description
                columns {
                    id
                    title
                    type
                }
                items_page(limit: 500) {
                    items {
                        id
                        name
                        state
                        created_at
                        updated_at
                        creator {
                            id
                            name
                            email
                        }
                        group {
                            id
                            title
                            color
                        }
                        column_values {
                            id
                            text
                            value
                            type
                            ... on BoardRelationValue {
                                linked_item_ids
                                display_value
                            }
                            ... on MirrorValue {
                                display_value
                            }
                        }
                        subitems {
                            id
                            name
                            state
                            created_at
                            updated_at
                            column_values {
                                id
                                text
                                value
                                type
                                ... on MirrorValue {
                                    display_value
                                }
                            }
                        }
                        updates {
                            id
                            body
                            created_at
                            creator {
                                id
                                name
                            }
                        }
                    }
                }
            }
        }
        """
        
        variables = {"board_id": [int(board_id)]}
        
        try:
            data = self._execute_query(query, variables)
            board_data = data.get('boards', [{}])[0]
            with open(f'monday_board_{board_id}_full_data.json', 'w') as f:
                json.dump(board_data, f, indent=4)
            items = board_data.get('items_page', {}).get('items', [])
            print(f"Total items found on board {board_id}: {len(items)}")
            
            # Create column ID to name mapping
            column_mapping = {}
            for col in board_data.get('columns', []):
                column_mapping[col['id']] = {
                    'title': col['title'],
                    'type': col['type']
                }
            
            # save top 10 items to JSON for inspection
            if save_to_json:
                sample_items = items[:10]
                with open(f'monday_board_{board_id}_sample_items.json', 'w') as f:
                    json.dump(sample_items, f, indent=4)
                print(f"Saved sample items to monday_board_{board_id}_sample_items.json")
            stories = []
            first_story_logged = False
            for item in items:
                # Build column dictionary with full data and extract linked_item_ids
                columns = {}
                columns_by_name = {}  # Additional mapping by column name
                for cv in item.get('column_values', []):
                    col_info = column_mapping.get(cv['id'], {})
                    col_name = col_info.get('title', cv['id'])
                    
                    col_data = {
                        'id': cv['id'],
                        'name': col_name,
                        'text': cv.get('text', ''),
                        'value': cv.get('value', ''),
                        'type': cv.get('type', '')
                    }
                    # Add linked_item_ids if present (for board_relation columns)
                    if 'linked_item_ids' in cv:
                        col_data['linked_item_ids'] = cv.get('linked_item_ids', [])
                    # Add display_value for board_relation and mirror columns
                    if 'display_value' in cv:
                        col_data['display_value'] = cv.get('display_value', '')
                        # For mirror columns, use display_value as text if text is null
                        if cv.get('type') == 'mirror' and not col_data['text']:
                            col_data['text'] = cv.get('display_value', '')
                    
                    columns[cv['id']] = col_data
                    columns_by_name[col_name] = col_data
                    
                    # Add standardized names for hours tracking (inside the loop)
                    if cv['id'] == 'task_estimation':
                        columns_by_name['Estimated Effort'] = col_data
                    elif cv['id'] == 'task_actual_effort':
                        columns_by_name['Actual Effort'] = col_data
                
                # Check if item is linked to this sprint
                is_in_sprint = False
                
                # Check task_sprint column specifically (board relation column)
                if 'task_sprint' in columns:
                    col_data = columns['task_sprint']
                    # Check linked_item_ids directly from the API response
                    linked_ids = col_data.get('linked_item_ids', [])
                    if str(sprint_id) in [str(lid) for lid in linked_ids]:
                        is_in_sprint = True
                
                # Fallback: check any board_relation column with 'sprint' in name
                if not is_in_sprint:
                    for col_id, col_data in columns.items():
                        if 'sprint' in col_id.lower() and col_data.get('type') == 'board_relation':
                            linked_ids = col_data.get('linked_item_ids', [])
                            if str(sprint_id) in [str(lid) for lid in linked_ids]:
                                is_in_sprint = True
                                break
                
                if is_in_sprint:
                    # Apply status filter if provided
                    if status_filter:
                        status = columns.get('status', {}).get('text', '')
                        if status_filter.lower() not in status.lower():
                            continue
                    
                    # Comprehensive story data structure
                    story = {
                        'id': item['id'],
                        'name': item['name'],
                        'state': item.get('state', ''),
                        'created_at': item.get('created_at', ''),
                        'updated_at': item.get('updated_at', ''),
                        'creator': item.get('creator', {}),
                        'group': {
                            'id': item.get('group', {}).get('id', ''),
                            'title': item.get('group', {}).get('title', ''),
                            'color': item.get('group', {}).get('color', '')
                        },
                        'board': {
                            'id': board_data.get('id', ''),
                            'name': board_data.get('name', ''),
                            'description': board_data.get('description', '')
                        },
                        # Quick access fields
                        'status': columns.get('task_status', {}).get('text', '') or columns.get('status', {}).get('text', ''),
                        'epic': columns.get('task_epic', {}).get('display_value', '') or columns.get('epic', {}).get('text', ''),
                        'client': (columns_by_name.get('Customers', {}).get('display_value', '') or 
                                  columns_by_name.get('Customers', {}).get('text', '')),
                        'product': (columns_by_name.get('Products', {}).get('display_value', '') or 
                                   columns_by_name.get('Products', {}).get('text', '') or
                                   self._extract_product_from_columns(columns_by_name)),
                        'priority': columns.get('priority', {}).get('text', ''),
                        'assignee': columns.get('task_owner', {}).get('text', '') or columns.get('people', {}).get('text', '') or columns.get('assignee', {}).get('text', ''),
                        'due_date': columns.get('due_date', {}).get('text', '') or columns.get('date', {}).get('text', ''),
                        'timeline': columns.get('timeline', {}).get('text', ''),
                        'story_points': columns.get('task_estimation', {}).get('text', '') or columns.get('story_points', {}).get('text', '') or columns.get('points', {}).get('text', ''),
                        'actual_effort': columns.get('task_actual_effort', {}).get('text', ''),
                        'sprint_info': columns.get('task_sprint', {}).get('display_value', ''),
                        # All columns with full data
                        'columns': columns,
                        'columns_by_name': columns_by_name,  # Access columns by their human-readable names
                        # Subitems with full details
                        'subitems': [
                            {
                                'id': sub.get('id', ''),
                                'name': sub.get('name', ''),
                                'state': sub.get('state', ''),
                                'created_at': sub.get('created_at', ''),
                                'updated_at': sub.get('updated_at', ''),
                                'column_values': {
                                    cv['id']: {
                                        'text': cv.get('text', '') or (cv.get('display_value', '') if cv.get('type') == 'mirror' else ''),
                                        'value': cv.get('value', ''),
                                        'type': cv.get('type', ''),
                                        'display_value': cv.get('display_value', '') if 'display_value' in cv else None
                                    } for cv in sub.get('column_values', [])
                                }
                            } for sub in item.get('subitems', [])
                        ],
                    }
                    
                    # Add updates
                    story['updates'] = [
                        {
                            'id': upd.get('id', ''),
                            'body': upd.get('body', ''),
                            'created_at': upd.get('created_at', ''),
                            'creator': upd.get('creator', {})
                        } for upd in item.get('updates', [])[:10]  # Limit to 10 most recent updates
                    ]
                    story['update_count'] = len(item.get('updates', []))
                    
                    # Log column names for first story to help debug workspace 2 structure
                    if not first_story_logged:
                        logger.info(f"  First story column names: {list(columns_by_name.keys())}")
                        logger.info(f"  Client: '{story['client']}', Product: '{story['product']}'")
                        first_story_logged = True
                    
                    stories.append(story)
            
            logger.info(f"Found {len(stories)} stories for sprint {sprint_id} on board {board_id}")
            
            # Save to JSON if requested
            if save_to_json and stories:
                limited_stories = stories[:limit]
                export_data = {
                    'export_info': {
                        'timestamp': datetime.now().isoformat(),
                        'board_id': board_id,
                        'board_name': board_data.get('name', ''),
                        'sprint_id': sprint_id,
                        'total_stories': len(stories),
                        'exported_count': len(limited_stories),
                        'status_filter': status_filter
                    },
                    'stories': limited_stories
                }
            
            return stories
        except Exception as e:
            logger.error(f"Error fetching stories by sprint: {str(e)}")
            return []
    
    def get_stories_from_user_stories_board(self, sprint_id: str, workspace_id: Optional[str] = None) -> List[Dict]:
        """
        Get all user stories and tasks from the Sprint Backlog board for a sprint.
        
        Args:
            sprint_id: Sprint item ID
            workspace_id: Optional specific workspace ID to search in
            
        Returns:
            List of all stories with client and product information
        """
        # Determine which workspace IDs to use
        target_workspace_ids = [workspace_id] if workspace_id else self.workspace_ids
        
        if not target_workspace_ids:
            query = """
            query {
                boards(limit: 500) {
                    id
                    name
                    workspace {
                        id
                    }
                }
            }
            """
            variables = None
        else:
            query = """
            query ($workspace_ids: [ID!]) {
                boards(limit: 500, workspace_ids: $workspace_ids) {
                    id
                    name
                    workspace {
                        id
                    }
                }
            }
            """
            variables = {"workspace_ids": [int(wid) for wid in target_workspace_ids]}
        
        try:
            data = self._execute_query(query, variables)
            user_stories_board_id = None
            for board in data.get('boards', []):
                if ('sprint backlog' in board['name'].lower() or 'user stories & tasks' in board['name'].lower() or 'tasks/user stories' in board['name'].lower()) and 'subitems' not in board['name'].lower():
                    user_stories_board_id = board['id']
                    print(f"Found Sprint Backlog board: {board['name']} (ID: {board['id']}, Workspace: {board.get('workspace', {}).get('id')})")
                    break
            
            if not user_stories_board_id:
                logger.warning(f"Sprint Backlog board not found in workspace {workspace_id if workspace_id else 'configured workspaces'}")
                return []
            
            return self.get_user_stories_by_sprint(user_stories_board_id, sprint_id)
        except Exception as e:
            logger.error(f"Error fetching stories from User Stories board: {str(e)}")
            return []

    def get_user_stories_board(self, workspace_id: str = None) -> Optional[Dict]:
        """
        Find the Sprint Backlog board in a workspace.
        
        Args:
            workspace_id: Optional workspace ID to search in
            
        Returns:
            Board dict with id, name, workspace, and columns, or None if not found
        """
        target_workspace_ids = [workspace_id] if workspace_id else self.workspace_ids
        
        query = """
        query ($workspace_ids: [ID!]) {
            boards(limit: 500, workspace_ids: $workspace_ids) {
                id
                name
                workspace {
                    id
                }
                columns {
                    id
                    title
                    type
                }
                groups {
                    id
                    title
                }
            }
        }
        """
        variables = {"workspace_ids": [int(wid) for wid in target_workspace_ids]}
        
        try:
            data = self._execute_query(query, variables)
            for board in data.get('boards', []):
                board_name_lower = board['name'].lower()
                if ('sprint backlog' in board_name_lower or 'user stories & tasks' in board_name_lower or 'tasks/user stories' in board_name_lower) and 'subitems' not in board_name_lower:
                    logger.info(f"Found Sprint Backlog board: {board['name']} (ID: {board['id']})")
                    return board
            
            logger.warning(f"Sprint Backlog board not found in workspace {workspace_id if workspace_id else 'configured workspaces'}")
            return None
        except Exception as e:
            logger.error(f"Error finding User Stories board: {str(e)}")
            return None

    def get_active_sprint_for_user_stories(self, workspace_id: str = None) -> Optional[Dict]:
        """
        Get the active sprint for the User Stories board.
        
        Args:
            workspace_id: Optional workspace ID
            
        Returns:
            Active sprint dict with id and name, or None
        """
        try:
            # First find the Sprints board
            sprints_board = self.get_sprints_board(workspace_id=workspace_id)
            if not sprints_board:
                logger.warning("Sprints board not found")
                return None
            
            # Get active sprint
            active_sprint = self.get_active_sprint(sprints_board['id'])
            return active_sprint
        except Exception as e:
            logger.error(f"Error getting active sprint for User Stories: {str(e)}")
            return None

    def create_item_with_columns(self, board_id: str, group_id: str, item_name: str, 
                                  column_values: Dict, create_labels_if_missing: bool = True) -> Optional[str]:
        """
        Create a new item on a board with column values.
        
        Args:
            board_id: Target board ID
            group_id: Target group ID
            item_name: Name for the new item
            column_values: Dict of column_id -> value mappings
            create_labels_if_missing: Whether to create missing labels for status columns
            
        Returns:
            New item ID or None
        """
        mutation = """
        mutation ($board_id: ID!, $group_id: String!, $item_name: String!, $column_values: JSON!, $create_labels: Boolean) {
            create_item(
                board_id: $board_id
                group_id: $group_id
                item_name: $item_name
                column_values: $column_values
                create_labels_if_missing: $create_labels
            ) {
                id
                name
            }
        }
        """
        
        import json
        column_values_json = json.dumps(column_values) if isinstance(column_values, dict) else column_values
        
        logger.info(f"Creating item '{item_name}' on board {board_id} with {len(column_values)} columns")
        logger.debug(f"Column values JSON: {column_values_json[:1000]}..." if len(column_values_json) > 1000 else f"Column values JSON: {column_values_json}")
        
        variables = {
            "board_id": str(board_id),
            "group_id": group_id,
            "item_name": item_name,
            "column_values": column_values_json,
            "create_labels": create_labels_if_missing
        }
        
        try:
            data = self._execute_query(mutation, variables)
            item_data = data.get('create_item', {})
            if item_data:
                logger.info(f"Successfully created item '{item_name}' with ID {item_data.get('id')}")
                return item_data.get('id')
            else:
                logger.error(f"create_item returned empty data")
            return None
        except Exception as e:
            logger.error(f"Error creating item with columns: {str(e)}")
            return None
            return None

    # ==================== COLUMN SYNC METHODS ====================
    
    def get_item_with_columns(self, item_id: str) -> Optional[Dict]:
        """
        Get item details including all column values and linked item IDs.
        
        Args:
            item_id: Monday.com item ID
            
        Returns:
            Item dict with id, name, board info, column_values, and subitems
        """
        query = """
        query ($item_ids: [ID!]) {
            items(ids: $item_ids) {
                id
                name
                board {
                    id
                    name
                }
                parent_item {
                    id
                    name
                }
                column_values {
                    id
                    text
                    value
                    type
                    ... on BoardRelationValue {
                        linked_item_ids
                        display_value
                    }
                }
                subitems {
                    id
                    name
                    column_values {
                        id
                        text
                        value
                        type
                    }
                }
            }
        }
        """
        
        variables = {"item_ids": [str(item_id)]}
        
        try:
            data = self._execute_query(query, variables)
            items = data.get('items', [])
            if items:
                return items[0]
            return None
        except Exception as e:
            logger.error(f"Error getting item with columns: {str(e)}")
            return None
    
    def get_board_columns(self, board_id: str) -> List[Dict]:
        """
        Get all columns for a board.
        
        Args:
            board_id: Monday.com board ID
            
        Returns:
            List of column dicts with id, title, and type
        """
        query = """
        query ($board_ids: [ID!]) {
            boards(ids: $board_ids) {
                columns {
                    id
                    title
                    type
                    settings_str
                }
            }
        }
        """
        
        variables = {"board_ids": [str(board_id)]}
        
        try:
            data = self._execute_query(query, variables)
            boards = data.get('boards', [])
            if boards:
                return boards[0].get('columns', [])
            return []
        except Exception as e:
            logger.error(f"Error getting board columns: {str(e)}")
            return []
    
    def get_board_by_id(self, board_id: str) -> Optional[Dict]:
        """
        Get board details by ID.
        
        Args:
            board_id: Monday.com board ID
            
        Returns:
            Board dict with id, name, workspace info
        """
        query = """
        query ($board_ids: [ID!]) {
            boards(ids: $board_ids) {
                id
                name
                workspace {
                    id
                    name
                }
            }
        }
        """
        
        variables = {"board_ids": [str(board_id)]}
        
        try:
            data = self._execute_query(query, variables)
            boards = data.get('boards', [])
            if boards:
                return boards[0]
            return None
        except Exception as e:
            logger.error(f"Error getting board by ID: {str(e)}")
            return None
    
    def get_column_settings(self, board_id: str, column_id: str) -> Optional[Dict]:
        """
        Get column settings including available labels for status/dropdown columns.
        
        Args:
            board_id: Monday.com board ID
            column_id: Column ID to get settings for
            
        Returns:
            Column settings dict or None
        """
        query = """
        query ($board_ids: [ID!]) {
            boards(ids: $board_ids) {
                columns {
                    id
                    title
                    type
                    settings_str
                }
            }
        }
        """
        
        variables = {"board_ids": [str(board_id)]}
        
        try:
            data = self._execute_query(query, variables)
            boards = data.get('boards', [])
            if boards:
                for col in boards[0].get('columns', []):
                    if col.get('id') == column_id:
                        settings_str = col.get('settings_str', '{}')
                        try:
                            settings = json.loads(settings_str)
                            return {
                                'id': col.get('id'),
                                'title': col.get('title'),
                                'type': col.get('type'),
                                'settings': settings
                            }
                        except json.JSONDecodeError:
                            return {
                                'id': col.get('id'),
                                'title': col.get('title'),
                                'type': col.get('type'),
                                'settings': {}
                            }
            return None
        except Exception as e:
            logger.error(f"Error getting column settings: {str(e)}")
            return None
    
    def find_matching_label(self, target_labels: dict, source_label: str) -> Optional[str]:
        """
        Find a matching label in target board using case-insensitive matching.
        
        Args:
            target_labels: Dict of index -> label text from target board
            source_label: The label text from source board
            
        Returns:
            The matching label text from target board, or None if no match
        """
        source_label_lower = source_label.lower().strip()
        
        for index, label_text in target_labels.items():
            if label_text.lower().strip() == source_label_lower:
                logger.info(f"Found matching label: '{source_label}' -> '{label_text}'")
                return label_text
        
        # Try partial matching as fallback
        for index, label_text in target_labels.items():
            if source_label_lower in label_text.lower().strip() or label_text.lower().strip() in source_label_lower:
                logger.info(f"Found partial matching label: '{source_label}' -> '{label_text}'")
                return label_text
        
        return None
    
    def update_column_value(self, board_id: str, item_id: str, column_id: str, value: str, column_type: str = None) -> bool:
        """
        Update a column value on an item.
        Handles status/color columns specially by matching labels case-insensitively.
        Handles numeric columns by extracting just the number value.
        
        Args:
            board_id: Monday.com board ID
            item_id: Monday.com item ID
            column_id: Column ID to update
            value: JSON string value for the column
            column_type: Optional column type for special handling
            
        Returns:
            True if successful, False otherwise
        """
        formatted_value = value
        source_label_text = None
        
        try:
            parsed_value = json.loads(value) if isinstance(value, str) else value
            
            if isinstance(parsed_value, dict):
                # Handle numeric columns - they expect just a number string, not an object
                # Format: {"value": 1, "unit": null} -> "1" or just the number
                if 'value' in parsed_value and ('unit' in parsed_value or column_type in ['numeric', 'numbers']):
                    numeric_val = parsed_value.get('value')
                    if numeric_val is not None:
                        # Monday.com numeric columns expect a simple string number
                        formatted_value = str(numeric_val)
                        logger.info(f"Extracted numeric value: {formatted_value}")
                    else:
                        # Value is null, set to empty
                        formatted_value = ""
                    # Skip label processing for numeric columns
                
                # Extract label text from various formats
                elif 'label' in parsed_value and isinstance(parsed_value['label'], dict):
                    source_label_text = parsed_value['label'].get('text')
                
                # Format 2: {"label": "Done"}
                elif 'label' in parsed_value and isinstance(parsed_value['label'], str):
                    source_label_text = parsed_value['label']
                
                # Format 3: {"text": "Done"}
                elif 'text' in parsed_value:
                    source_label_text = parsed_value['text']
                
                # Format 4: {"index": 1} - can't sync by index alone
                elif 'index' in parsed_value and 'label' not in parsed_value and 'text' not in parsed_value:
                    logger.warning(f"Cannot sync by index alone - need label text. Value: {value}")
                    return False
                
                # Format 5: Dropdown with labels array {"labels": ["Label1", "Label2"]}
                elif 'labels' in parsed_value and isinstance(parsed_value['labels'], list):
                    # For dropdowns, we need to match each label
                    source_labels = parsed_value['labels']
                    if source_labels:
                        # Get target column settings to find matching labels
                        target_col_settings = self.get_column_settings(board_id, column_id)
                        if target_col_settings and target_col_settings.get('settings', {}).get('labels'):
                            target_labels = target_col_settings['settings']['labels']
                            matched_labels = []
                            for src_label in source_labels:
                                matched = self.find_matching_label(target_labels, src_label)
                                if matched:
                                    matched_labels.append(matched)
                            if matched_labels:
                                formatted_value = json.dumps({"labels": matched_labels})
                                logger.info(f"Matched dropdown labels: {source_labels} -> {matched_labels}")
                
                # If we found a source label text, try to match it with target board
                if source_label_text:
                    # Get target column settings to find available labels
                    target_col_settings = self.get_column_settings(board_id, column_id)
                    
                    if target_col_settings:
                        settings = target_col_settings.get('settings', {})
                        target_labels = settings.get('labels', {})
                        
                        if target_labels:
                            # Find matching label (case-insensitive)
                            matched_label = self.find_matching_label(target_labels, source_label_text)
                            
                            if matched_label:
                                formatted_value = json.dumps({"label": matched_label})
                                logger.info(f"Using matched label: '{source_label_text}' -> '{matched_label}'")
                            else:
                                # No match found - log available labels
                                available = list(target_labels.values())
                                logger.warning(f"Label '{source_label_text}' not found in target. Available: {available}")
                                # Still try with original label - might work if exact match
                                formatted_value = json.dumps({"label": source_label_text})
                        else:
                            # No labels in settings, use original
                            formatted_value = json.dumps({"label": source_label_text})
                    else:
                        # Couldn't get settings, use original
                        formatted_value = json.dumps({"label": source_label_text})
                        
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug(f"Value is not JSON or couldn't be parsed: {e}")
            # Keep original value
        
        mutation = """
        mutation ($board_id: ID!, $item_id: ID!, $column_id: String!, $value: JSON!) {
            change_column_value(
                board_id: $board_id
                item_id: $item_id
                column_id: $column_id
                value: $value
            ) {
                id
            }
        }
        """
        
        variables = {
            "board_id": str(board_id),
            "item_id": str(item_id),
            "column_id": column_id,
            "value": formatted_value
        }
        
        try:
            self._execute_query(mutation, variables)
            logger.info(f"Updated column {column_id} on item {item_id}")
            return True
        except Exception as e:
            error_msg = str(e)
            if 'status label' in error_msg.lower() or 'missingLabel' in error_msg:
                logger.warning(f"Label not found in target board: {error_msg}")
            else:
                logger.error(f"Error updating column value: {error_msg}")
            return False
    
    def get_subitem_with_parent(self, subitem_id: str) -> Optional[Dict]:
        """
        Get subitem details including parent item info.
        
        Args:
            subitem_id: Monday.com subitem ID
            
        Returns:
            Subitem dict with parent_item info included
        """
        query = """
        query ($item_ids: [ID!]) {
            items(ids: $item_ids) {
                id
                name
                board {
                    id
                    name
                }
                parent_item {
                    id
                    name
                    board {
                        id
                        name
                    }
                    column_values {
                        id
                        text
                        value
                        type
                        ... on BoardRelationValue {
                            linked_item_ids
                            display_value
                        }
                    }
                }
                column_values {
                    id
                    text
                    value
                    type
                }
            }
        }
        """
        
        variables = {"item_ids": [str(subitem_id)]}
        
        try:
            data = self._execute_query(query, variables)
            items = data.get('items', [])
            if items:
                return items[0]
            return None
        except Exception as e:
            logger.error(f"Error getting subitem with parent: {str(e)}")
            return None
    
    def get_item_subitems(self, item_id: str) -> List[Dict]:
        """
        Get all subitems for an item.
        
        Args:
            item_id: Monday.com item ID
            
        Returns:
            List of subitem dicts with id, name, and column_values
        """
        query = """
        query ($item_ids: [ID!]) {
            items(ids: $item_ids) {
                subitems {
                    id
                    name
                    board {
                        id
                        name
                    }
                    column_values {
                        id
                        text
                        value
                        type
                        column {
                            title
                        }
                        ... on MirrorValue {
                            display_value
                        }
                    }
                }
            }
        }
        """
        
        variables = {"item_ids": [str(item_id)]}
        
        try:
            data = self._execute_query(query, variables)
            items = data.get('items', [])
            if items:
                return items[0].get('subitems', [])
            return []
        except Exception as e:
            logger.error(f"Error getting item subitems: {str(e)}")
            return []
    
    def get_subitem_board_id(self, parent_board_id: str) -> Optional[str]:
        """
        Get the subitems board ID for a parent board.
        
        Args:
            parent_board_id: Parent board ID
            
        Returns:
            Subitems board ID or None
        """
        query = """
        query ($board_ids: [ID!]) {
            boards(ids: $board_ids) {
                columns {
                    id
                    type
                    settings_str
                }
            }
        }
        """
        
        variables = {"board_ids": [str(parent_board_id)]}
        
        try:
            data = self._execute_query(query, variables)
            boards = data.get('boards', [])
            if boards:
                for col in boards[0].get('columns', []):
                    if col.get('type') == 'subtasks':
                        # Parse settings to get linked board ID
                        settings = col.get('settings_str', '{}')
                        try:
                            import json
                            settings_dict = json.loads(settings)
                            return settings_dict.get('boardId')
                        except:
                            pass
            return None
        except Exception as e:
            logger.error(f"Error getting subitem board ID: {str(e)}")
            return None
    
    def find_boards_by_type(self, workspace_id: str, board_type: str) -> List[Dict]:
        """
        Find boards of a specific type in a workspace.
        
        Args:
            workspace_id: Workspace ID to search in
            board_type: Type of board ('fast_lane', 'bugs_queue', 'user_stories')
            
        Returns:
            List of matching board dicts
        """
        from utils.helper import BOARD_PATTERNS
        
        query = """
        query ($workspace_ids: [ID!]) {
            boards(limit: 500, workspace_ids: $workspace_ids) {
                id
                name
                workspace {
                    id
                }
            }
        }
        """
        
        variables = {"workspace_ids": [int(workspace_id)]}
        
        try:
            data = self._execute_query(query, variables)
            boards = data.get('boards', [])
            
            patterns = BOARD_PATTERNS.get(board_type, [])
            matching_boards = []
            
            for board in boards:
                board_name_lower = board['name'].lower()
                # Skip subitems boards
                if 'subitems' in board_name_lower:
                    continue
                for pattern in patterns:
                    if pattern in board_name_lower:
                        matching_boards.append(board)
                        break
            
            return matching_boards
        except Exception as e:
            logger.error(f"Error finding boards by type: {str(e)}")
            return []

    def create_subitem(self, parent_item_id: str, subitem_name: str, column_values: Optional[Dict] = None) -> Optional[str]:
        """
        Create a subitem under a parent item.
        
        Args:
            parent_item_id: ID of the parent item
            subitem_name: Name for the new subitem
            column_values: Optional dict of column_id -> JSON value mappings
            
        Returns:
            Created subitem ID or None if failed
        """
        if column_values:
            mutation = """
            mutation ($parent_item_id: ID!, $item_name: String!, $column_values: JSON!) {
                create_subitem(
                    parent_item_id: $parent_item_id
                    item_name: $item_name
                    column_values: $column_values
                ) {
                    id
                    name
                    board {
                        id
                    }
                }
            }
            """
            variables = {
                "parent_item_id": str(parent_item_id),
                "item_name": subitem_name,
                "column_values": json.dumps(column_values)
            }
        else:
            mutation = """
            mutation ($parent_item_id: ID!, $item_name: String!) {
                create_subitem(
                    parent_item_id: $parent_item_id
                    item_name: $item_name
                ) {
                    id
                    name
                    board {
                        id
                    }
                }
            }
            """
            variables = {
                "parent_item_id": str(parent_item_id),
                "item_name": subitem_name
            }
        
        try:
            data = self._execute_query(mutation, variables)
            subitem_data = data.get('create_subitem', {})
            subitem_id = subitem_data.get('id')
            if subitem_id:
                logger.info(f"Created subitem '{subitem_name}' (ID: {subitem_id}) under parent {parent_item_id}")
                return subitem_id
            return None
        except Exception as e:
            logger.error(f"Error creating subitem: {str(e)}")
            return None
    
    def get_subitem_with_all_columns(self, subitem_id: str) -> Optional[Dict]:
        """
        Get subitem with all column values for syncing.
        
        Args:
            subitem_id: Monday.com subitem ID
            
        Returns:
            Subitem dict with all column values
        """
        query = """
        query ($item_ids: [ID!]) {
            items(ids: $item_ids) {
                id
                name
                board {
                    id
                    name
                }
                column_values {
                    id
                    text
                    value
                    type
                }
            }
        }
        """
        
        variables = {"item_ids": [str(subitem_id)]}
        
        try:
            data = self._execute_query(query, variables)
            items = data.get('items', [])
            if items:
                return items[0]
            return None
        except Exception as e:
            logger.error(f"Error getting subitem with all columns: {str(e)}")
            return None

