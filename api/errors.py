import logging

from flask import Blueprint, jsonify


logger = logging.getLogger(__name__)
bp = Blueprint("errors", __name__)

# ==================== ERROR HANDLERS ====================

@bp.app_errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not Found', 'status': 'error'}), 404

@bp.app_errorhandler(500)
def server_error(error):
    logger.error(f"Server error: {str(error)}")
    return jsonify({'error': 'Internal Server Error', 'status': 'error'}), 500
