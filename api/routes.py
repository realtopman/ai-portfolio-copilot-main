from api.context import init_integrations
from api.errors import bp as errors_bp
from api.health import bp as health_bp
from api.monday_sync import bp as monday_sync_bp
from api.reports import bp as reports_bp
from api.timebuzzer import bp as timebuzzer_bp
from api.transfer import bp as transfer_bp


def register_blueprints(app):
    """Register all API blueprints."""
    app.register_blueprint(health_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(monday_sync_bp)
    app.register_blueprint(timebuzzer_bp)
    app.register_blueprint(transfer_bp)
    app.register_blueprint(errors_bp)


__all__ = ["init_integrations", "register_blueprints"]
