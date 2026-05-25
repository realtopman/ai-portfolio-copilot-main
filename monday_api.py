"""
Monday.com API integration module for fetching and managing board data.
"""

import requests
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import logging

logger = logging.getLogger(__name__)


class MondayAPI:
    """Handles all interactions with Monday.com API."""
    
    def __init__(self, api_token: str, api_url: str = 'https://api.monday.com/v2', workspace_ids: List[str] = None):
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
    
    def get_board_id_by_name(self, board_name: str, workspace_id: Optional[str] = None) -> Optional[str]:
        """
        Get board ID by board name.
        
        Args:
            board_name: Name of the board
            workspace_id: Optional specific workspace ID to search in
            
        Returns:
            Board ID or None if not found
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
            for board in data.get('boards', []):
                if board['name'] == board_name:
                    return board['id']
            return None
        except Exception as e:
            logger.error(f"Error fetching board: {str(e)}")
            return None
    
    def get_completed_user_stories(self, board_id: str, sprint_id: str) -> List[Dict]:
        """
        Get completed user stories for a specific sprint on a board.
        
        Args:
            board_id: Monday.com board ID
            sprint_id: Sprint ID to filter by
            
        Returns:
            List of completed user stories with their details
        """
        query = """
        query ($board_id: [ID!]) {
            boards(ids: $board_id) {
                items_page(limit: 100) {
                    items {
                        id
                        name
                        state
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
        
        variables = {"board_id": [int(board_id)]}
        
        try:
            data = self._execute_query(query, variables)
            items = data.get('boards', [{}])[0].get('items_page', {}).get('items', [])
            
            # Filter for completed items linked to the sprint
            completed_stories = []
            for item in items:
                columns = {cv['id']: cv['text'] or cv['value'] for cv in item.get('column_values', [])}
                
                # Check if item is marked as "Done" and linked to the sprint
                if columns.get('status', '').lower() == 'done' or \
                   columns.get('state', '').lower() == 'done':
                    if sprint_id in json.dumps(columns):
                        completed_stories.append({
                            'id': item['id'],
                            'name': item['name'],
                            'status': columns.get('status', ''),
                            'epic': columns.get('epic', ''),
                            'columns': columns
                        })
            
            return completed_stories
        except Exception as e:
            logger.error(f"Error fetching completed stories: {str(e)}")
            return []
    
    def get_planned_user_stories(self, board_id: str, sprint_id: str) -> List[Dict]:
        """
        Get planned user stories for a specific sprint.
        
        Args:
            board_id: Monday.com board ID
            sprint_id: Sprint ID to filter by
            
        Returns:
            List of planned user stories
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
        
        variables = {"board_id": [int(board_id)]}
        
        try:
            data = self._execute_query(query, variables)
            items = data.get('boards', [{}])[0].get('items_page', {}).get('items', [])
            
            # Filter for non-completed items linked to the sprint
            planned_stories = []
            for item in items:
                columns = {cv['id']: cv['text'] or cv['value'] for cv in item.get('column_values', [])}
                
                # Check if item is NOT done and linked to the sprint
                if columns.get('status', '').lower() != 'done' and \
                   columns.get('state', '').lower() != 'done' and \
                   sprint_id in json.dumps(columns):
                    planned_stories.append({
                        'id': item['id'],
                        'name': item['name'],
                        'status': columns.get('status', ''),
                        'epic': columns.get('epic', ''),
                        'columns': columns
                    })
            
            return planned_stories
        except Exception as e:
            logger.error(f"Error fetching planned stories: {str(e)}")
            return []
    
    def get_sprint_info(self, board_id: str) -> List[Dict]:
        """
        Get all sprints for a board.
        
        Args:
            board_id: Monday.com board ID
            
        Returns:
            List of sprint information
        """
        query = """
        query ($board_id: [ID!]) {
            boards(ids: $board_id) {
                items_page(limit: 100) {
                    items {
                        id
                        name
                    }
                }
            }
        }
        """
        
        variables = {"board_id": [int(board_id)]}
        
        try:
            data = self._execute_query(query, variables)
            # This is a simplified version - adjust based on your actual sprint storage
            return data.get('boards', [{}])[0].get('items_page', {}).get('items', [])
        except Exception as e:
            logger.error(f"Error fetching sprint info: {str(e)}")
            return []
    
    def create_item_on_board(self, board_id: str, group_id: str, item_name: str, 
                           column_values: Optional[Dict] = None) -> Optional[str]:
        """
        Create a new item on a Monday board in a specific group.
        
        Args:
            board_id: Monday.com board ID
            group_id: Group ID (e.g., "Weekly Report" or "Incoming Request/Bugs")
            item_name: Name of the item to create
            column_values: Dictionary of column_id -> value mappings
            
        Returns:
            Created item ID or None if failed
        """
        # Format column values for mutation
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
    
    def update_item_field(self, item_id: str, column_id: str, value: Any) -> bool:
        """
        Update a specific field on an item.
        
        Args:
            item_id: Monday.com item ID
            column_id: Column ID to update
            value: New value for the field
            
        Returns:
            True if successful, False otherwise
        """
        mutation = f"""
        mutation {{
            change_column_value(
                item_id: {item_id}
                column_id: "{column_id}"
                value: {json.dumps({"value": value})}
            ) {{
                id
            }}
        }}
        """
        
        try:
            self._execute_query(mutation)
            logger.info(f"Updated item {item_id} field {column_id}")
            return True
        except Exception as e:
            logger.error(f"Error updating item field: {str(e)}")
            return False
    
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
                # Match by name
                if item['name'] == item_name:
                    # If group_id is specified, also check the group
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
    
    def create_doc(self, workspace_id: int, title: str, content: str) -> Optional[str]:
        """
        Create a Monday.com doc in a workspace.
        
        Args:
            workspace_id: Monday.com workspace ID
            title: Title of the document
            content: Content of the document (markdown format)
            
        Returns:
            Doc ID or None if failed
        """
        # First, create the doc
        create_mutation = f"""
        mutation {{
            create_doc(
                location: {{
                    workspace: {{
                        workspace_id: {workspace_id}
                        name: "{title}"
                    }}
                }}
            ) {{
                id
            }}
        }}
        """
        
        try:
            data = self._execute_query(create_mutation)
            doc_id = data.get('create_doc', {}).get('id')
            
            if not doc_id:
                logger.error("Failed to create doc: no ID returned")
                return None
                
            logger.info(f"Created doc {doc_id}")
            
            # Add content blocks to the doc
            try:
                # Add title block
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
                
                self._execute_query(title_mutation, title_variables)
                
                # Add content block
                content_variables = {
                    "doc_id": doc_id,
                    "content": json.dumps({
                        "deltaFormat": [{"insert": content}]
                    })
                }
                
                self._execute_query(title_mutation, content_variables)
                logger.info(f"Added content to doc {doc_id}")
                
            except Exception as content_error:
                logger.warning(f"Could not add content to doc: {str(content_error)}")
                # Still return the doc_id even if content addition fails
            
            return doc_id
            
        except Exception as e:
            logger.error(f"Error creating doc: {str(e)}")
            return None
    
    def update_doc_column(self, board_id: str, item_id: str, column_id: str, doc_id: str) -> bool:
        """
        Update a doc column on an item with a doc ID.
        
        Args:
            board_id: Monday.com board ID
            item_id: Monday.com item ID
            column_id: Column ID of the doc column
            doc_id: Doc ID to link
            
        Returns:
            True if successful, False otherwise
        """
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
            "board_id": board_id,
            "item_id": item_id,
            "column_id": column_id,
            "value": json.dumps({"doc_id": int(doc_id)})
        }
        
        try:
            self._execute_query(mutation, variables)
            logger.info(f"Updated doc column {column_id} on item {item_id} with doc {doc_id}")
            return True
        except Exception as e:
            logger.error(f"Error updating doc column: {str(e)}")
            return False
    
    def create_doc_in_column(self, board_id: str, item_id: str, column_id: str, content: str) -> bool:
        """
        Create doc content directly in a doc column (no separate workspace doc).
        
        Args:
            board_id: Monday.com board ID
            item_id: Monday.com item ID
            column_id: Column ID of the doc column
            content: Text content to add to the doc
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Step 1: Initialize the doc column by updating it with an empty doc object
            # This forces Monday.com to create a doc for this column
            init_mutation = """
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
            
            init_variables = {
                "board_id": int(board_id),
                "item_id": int(item_id),
                "column_id": column_id,
                "value": json.dumps({})  # Empty object to initialize the doc
            }
            
            self._execute_query(init_mutation, init_variables)
            logger.info(f"Initialized doc column {column_id} on item {item_id}")
            
            # Step 2: Query the item again to get the newly created doc_id
            query = """
            query ($item_id: [ID!]) {
                items(ids: $item_id) {
                    id
                    column_values {
                        id
                        value
                        type
                    }
                }
            }
            """
            
            variables = {"item_id": [int(item_id)]}
            data = self._execute_query(query, variables)
            
            # Get the doc_id from the column value
            doc_id = None
            items = data.get('items', [])
            if items:
                column_values = items[0].get('column_values', [])
                logger.info(f"Retrieved {len(column_values)} column values for item {item_id}")
                for cv in column_values:
                    if cv['id'] == column_id:
                        logger.info(f"Found column {column_id}, type: {cv.get('type')}, value: {cv.get('value')}")
                        if cv.get('value'):
                            try:
                                value_data = json.loads(cv['value'])
                                logger.info(f"Parsed column value: {value_data}")
                                # Doc columns have structure like {"doc": {"id": 123456}} or {"doc_id": 123456}
                                if isinstance(value_data, dict):
                                    doc_id = value_data.get('doc', {}).get('id')
                                    if not doc_id:
                                        doc_id = value_data.get('doc_id')
                                    if not doc_id and 'id' in value_data:
                                        doc_id = value_data.get('id')
                            except Exception as parse_error:
                                logger.error(f"Error parsing column value: {parse_error}")
                        else:
                            logger.warning(f"Column {column_id} still has no value after initialization")
            
            if not doc_id:
                logger.error(f"Could not retrieve doc_id from column {column_id} after initialization. Column value: {cv.get('value') if cv else 'N/A'}")
                return False
            
            logger.info(f"Found doc_id {doc_id} for column {column_id}")
            
            # Step 3: Add content to the doc
            content_mutation = """
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
            
            content_variables = {
                "doc_id": str(doc_id),
                "content": json.dumps({
                    "deltaFormat": [{"insert": content}]
                })
            }
            
            self._execute_query(content_mutation, content_variables)
            logger.info(f"Added content to doc column {column_id} on item {item_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error creating doc in column: {str(e)}")
            return False
    
    def get_group_id_by_name(self, board_id: str, group_name: str) -> Optional[str]:
        """
        Get group ID by group name.
        
        Args:
            board_id: Monday.com board ID
            group_name: Name of the group (e.g., "Weekly Report")
            
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
    
    def get_column_id_by_title(self, board_id: str, column_title: str) -> Optional[str]:
        """
        Get column ID by column title.
        
        Args:
            board_id: Monday.com board ID
            column_title: Title of the column (e.g., "Report")
            
        Returns:
            Column ID or None if not found
        """
        query = """
        query ($board_id: [ID!]) {
            boards(ids: $board_id) {
                columns {
                    id
                    title
                }
            }
        }
        """
        
        variables = {"board_id": [int(board_id)]}
        
        try:
            data = self._execute_query(query, variables)
            columns = data.get('boards', [{}])[0].get('columns', [])
            for column in columns:
                if column['title'].lower() == column_title.lower():
                    return column['id']
            return None
        except Exception as e:
            logger.error(f"Error fetching columns: {str(e)}")
            return None
    
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
    
    def create_doc_in_folder(self, workspace_id: str, folder_id: str, title: str, content: str) -> Optional[str]:
        """
        Create a Monday.com doc in a specific folder.
        
        Args:
            workspace_id: Monday.com workspace ID
            folder_id: Monday.com folder ID
            title: Title of the document
            content: Content of the document (text format)
            
        Returns:
            Doc ID or None if failed
        """
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
            "workspace_id": int(workspace_id),
            "folder_id": int(folder_id),
            "title": title
        }
        
        try:
            data = self._execute_query(create_mutation, variables)
            doc_id = data.get('create_doc', {}).get('id')
            
            if not doc_id:
                logger.error("Failed to create doc in folder: no ID returned")
                return None
                
            logger.info(f"Created doc {doc_id} in folder {folder_id}")
            
            # Add content blocks to the doc
            try:
                # Add title block
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
                
                self._execute_query(title_mutation, title_variables)
                
                # Add content block
                content_variables = {
                    "doc_id": doc_id,
                    "content": json.dumps({
                        "deltaFormat": [{"insert": content}]
                    })
                }
                
                self._execute_query(title_mutation, content_variables)
                logger.info(f"Added content to doc {doc_id}")
                
            except Exception as content_error:
                logger.warning(f"Could not add content to doc: {str(content_error)}")
                # Still return the doc_id even if content addition fails
            
            return doc_id
            
        except Exception as e:
            logger.error(f"Error creating doc in folder: {str(e)}")
            return None
    
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
                        self._execute_query(separator_mutation, separator_variables)
                        logger.info(f"    ✓ Table title separator added")
                        
                        # Create the table block
                        self._create_table_block(doc_id, table_rows)
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
    
    
    def _create_table_block(self, doc_id: str, table_rows: List[Dict]) -> None:
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
            mutation ($doc_id: ID!, $type: DocBlockContentType!, $content: JSON!) {
                create_doc_block(
                    doc_id: $doc_id
                    type: $type
                    content: $content
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
                "content": table_content
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
        table_text += "📊 WEEKLY REPORT SUMMARY TABLE\n"
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
            
            for item in items:
                columns = {cv['id']: cv for cv in item.get('column_values', [])}
                
                # Check for sprint_activation column specifically
                if 'sprint_activation' in columns:
                    col_data = columns['sprint_activation']
                    value = col_data.get('value', '')
                    print(f"  sprint_activation value: {value}")
                    
                    # Parse the JSON value and check if checked is true
                    if value and '"checked":true' in value:
                        logger.info(f"Found active sprint: {item['name']}")
                        return {
                            'id': item['id'],
                            'name': item['name'],
                            'columns': columns
                        }
                
                # Fallback: check for any column with 'active' or 'current' in name
                for col_id, col_data in columns.items():
                    if 'active' in col_id.lower() or 'current' in col_id.lower():
                        value = col_data.get('value', '')
                        text = col_data.get('text', '')
                        if '"checked":true' in value or text.lower() == 'true' or 'true' in str(value).lower():
                            logger.info(f"Found active sprint: {item['name']}")
                            return {
                                'id': item['id'],
                                'name': item['name'],
                                'columns': columns
                            }
            
            logger.warning("No active sprint found")
            return None
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
                        'client': columns_by_name.get('Customer', {}).get('display_value', '') or columns_by_name.get('Customer', {}).get('text', ''),
                        'product': columns_by_name.get('Product', {}).get('display_value', '') or columns_by_name.get('Product', {}).get('text', ''),
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
                        # Updates/comments
                        'updates': [
                            {
                                'id': upd.get('id', ''),
                                'body': upd.get('body', ''),
                                'created_at': upd.get('created_at', ''),
                                'creator': upd.get('creator', {})
                            } for upd in item.get('updates', [])[:10]  # Limit to 10 most recent updates
                        ],
                        'update_count': len(item.get('updates', []))
                    }
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
        Get all user stories and tasks from the User Stories & Tasks board for a sprint.
        
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
                if ('user stories & tasks' in board['name'].lower() or 'tasks/user stories' in board['name'].lower()) and 'subitems' not in board['name'].lower():
                    user_stories_board_id = board['id']
                    print(f"Found User Stories board: {board['name']} (ID: {board['id']}, Workspace: {board.get('workspace', {}).get('id')})")
                    break
            
            if not user_stories_board_id:
                logger.warning(f"User Stories & Tasks board not found in workspace {workspace_id if workspace_id else 'configured workspaces'}")
                return []
            
            return self.get_user_stories_by_sprint(user_stories_board_id, sprint_id)
        except Exception as e:
            logger.error(f"Error fetching stories from User Stories board: {str(e)}")
            return []
