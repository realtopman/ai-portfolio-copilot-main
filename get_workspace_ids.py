"""
Helper script to find your Monday.com workspace IDs.
Run this script to get all workspace IDs in your account.
"""

import os
from dotenv import load_dotenv
import requests
import json

# Load environment variables
load_dotenv()

MONDAY_API_TOKEN = os.environ.get('MONDAY_API_TOKEN')
MONDAY_API_URL = 'https://api.monday.com/v2'

def get_workspaces():
    """Fetch all workspaces from Monday.com."""
    query = """
    query {
        workspaces {
            id
            name
            description
        }
    }
    """
    
    headers = {
        'Authorization': MONDAY_API_TOKEN,
        'Content-Type': 'application/json'
    }
    
    response = requests.post(
        MONDAY_API_URL,
        json={'query': query},
        headers=headers
    )
    
    if response.status_code == 200:
        data = response.json()
        if 'errors' in data:
            print(f"Error: {data['errors']}")
            return None
        return data.get('data', {}).get('workspaces', [])
    else:
        print(f"HTTP Error {response.status_code}: {response.text}")
        return None

def get_boards_by_workspace(workspace_id):
    """Fetch boards for a specific workspace."""
    query = """
    query {
        boards(limit: 100, workspace_ids: [%s]) {
            id
            name
        }
    }
    """ % workspace_id
    
    headers = {
        'Authorization': MONDAY_API_TOKEN,
        'Content-Type': 'application/json'
    }
    
    response = requests.post(
        MONDAY_API_URL,
        json={'query': query},
        headers=headers
    )
    
    if response.status_code == 200:
        data = response.json()
        if 'errors' in data:
            return []
        return data.get('data', {}).get('boards', [])
    return []

def main():
    """Main function to display workspace information."""
    if not MONDAY_API_TOKEN:
        print("ERROR: MONDAY_API_TOKEN not found in environment variables!")
        print("Please add it to your .env file")
        return
    
    print("Fetching workspaces from Monday.com...\n")
    workspaces = get_workspaces()
    
    if not workspaces:
        print("No workspaces found or error occurred.")
        return
    
    print("=" * 80)
    print("YOUR MONDAY.COM WORKSPACES")
    print("=" * 80)
    
    for idx, workspace in enumerate(workspaces, 1):
        print(f"\n{idx}. Workspace: {workspace['name']}")
        print(f"   ID: {workspace['id']}")
        if workspace.get('description'):
            print(f"   Description: {workspace['description']}")
        
        # Fetch boards for this workspace
        print(f"   Fetching boards...")
        boards = get_boards_by_workspace(workspace['id'])
        
        if boards:
            print(f"   Boards ({len(boards)}):")
            for board in boards[:10]:  # Show first 10 boards
                print(f"     - {board['name']} (ID: {board['id']})")
            if len(boards) > 10:
                print(f"     ... and {len(boards) - 10} more boards")
    
    print("\n" + "=" * 80)
    print("CONFIGURATION INSTRUCTIONS")
    print("=" * 80)
    print("\nAdd these to your .env file:")
    print("\n# Choose which workspace to use (replace with actual IDs)")
    if len(workspaces) >= 1:
        print(f"WORKSPACE_ID_1={workspaces[0]['id']}")
    if len(workspaces) >= 2:
        print(f"WORKSPACE_ID_2={workspaces[1]['id']}")
    if len(workspaces) >= 1:
        print(f"ACTIVE_WORKSPACE_ID={workspaces[0]['id']}  # Set to workspace you want to use")
    
    print("\n# To switch workspaces, just change ACTIVE_WORKSPACE_ID")
    print("# The system will only show boards from the active workspace")
    print("\n" + "=" * 80)

if __name__ == '__main__':
    main()
