from flask import Blueprint, jsonify

from api import context


bp = Blueprint("health", __name__)

# ==================== EXAMPLE ROUTES ====================

@bp.route('/')
def index():
    """Home endpoint."""
    return jsonify({
        'message': 'Sprint Report API Server',
        'status': 'active',
        'version': '1.0.0'
    })

@bp.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint."""

    print("""Performing health check...""")
    integrations_status = {
        'monday_api' : context.monday_api is not None,
        'openai': context.report_generator is not None
    }
    return jsonify({
        'status': 'healthy',
        'integrations': integrations_status
    })
