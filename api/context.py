import logging

from flask import current_app

from services.monday_api import MondayAPI
from services.openai_helper import ReportGenerator


logger = logging.getLogger(__name__)

monday_api = None
report_generator = None


def init_integrations():
    """Initialize external API integrations."""
    global monday_api, report_generator

    monday_token = current_app.config.get('MONDAY_API_TOKEN')
    openai_key = current_app.config.get('OPENAI_API_KEY')
    logger.info("Initializing integrations...")
    logger.info(f"MONDAY_API_TOKEN present: {bool(monday_token)}")
    logger.info(f"OPENAI_API_KEY present: {bool(openai_key)}")

    if monday_token:
        # Preserve the previous behavior: only WORKSPACE_ID_1 is used for the
        # default MondayAPI workspace filter. Other code can still pass an
        # explicit workspace_id when needed.
        workspace_ids = []
        workspace_1 = current_app.config.get('WORKSPACE_ID_1')

        if workspace_1:
            workspace_ids.append(workspace_1)

        monday_api = MondayAPI(
            api_token=monday_token,
            api_url=current_app.config['MONDAY_API_URL'],
            workspace_ids=workspace_ids,
        )
        logger.info(f"Monday API initialized successfully with workspace filter: {workspace_ids}")
    else:
        logger.error("MONDAY_API_TOKEN not found in config!")

    if openai_key:
        try:
            report_generator = ReportGenerator(
                api_key=openai_key,
                model=current_app.config.get('OPENAI_MODEL', 'gpt-3.5-turbo'),
            )
            logger.info("OpenAI API initialized successfully")
        except Exception as e:
            logger.warning(f"OpenAI initialization failed: {str(e)}. Will use fallback reports.")
            report_generator = None
    else:
        logger.info("OpenAI API key not provided. Will use fallback reports.")
