import os

from dotenv import load_dotenv

load_dotenv()

from flask import Flask

from api.routes import init_integrations
from api.routes import register_blueprints
from config import config


def create_app():
    """Create and configure the Flask automation server."""
    env = os.environ.get("FLASK_ENV", "development")
    app = Flask(__name__)
    app.config.from_object(config[env])
    register_blueprints(app)

    with app.app_context():
        init_integrations()

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        debug=True,
        host="0.0.0.0",
        port=5002,
        use_reloader=False,
        threaded=True,
    )
