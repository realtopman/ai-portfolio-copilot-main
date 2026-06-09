import json


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
