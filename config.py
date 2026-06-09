import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

class Config:
    """Base configuration"""
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key'
    JSON_SORT_KEYS = False
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    
    # Monday.com Configuration
    MONDAY_API_TOKEN = os.environ.get('MONDAY_API_TOKEN')
    MONDAY_API_URL = 'https://api.monday.com/v2'
    
    # Workspace IDs (static configuration for two workspaces)
    WORKSPACE_ID_1 = os.environ.get('WORKSPACE_ID_1', '4840765')  # First workspace ID
    WORKSPACE_ID_2 = os.environ.get('WORKSPACE_ID_2', '5109455')  # Second workspace ID
    
    # OpenAI Configuration
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-3.5-turbo')

    # TimeBuzzer Configuration
    TIMEBUZZER_API_KEY = os.environ.get('TIMEBUZZER_API_KEY')
    TIMEBUZZER_BASE_URL = os.environ.get('TIMEBUZZER_BASE_URL', 'https://my.timebuzzer.com/open-api')
    TIMEBUZZER_LAYER_IDS = os.environ.get('TIMEBUZZER_LAYER_IDS')
    TIMEBUZZER_EPIC_LAYER_ID = os.environ.get('TIMEBUZZER_EPIC_LAYER_ID')
    TIMEBUZZER_ITEM_LAYER_ID = os.environ.get('TIMEBUZZER_ITEM_LAYER_ID')
    TIMEBUZZER_SUBITEM_LAYER_ID = os.environ.get('TIMEBUZZER_SUBITEM_LAYER_ID')
    TIMEBUZZER_ACTIVITY_CACHE_FILE = os.environ.get('TIMEBUZZER_ACTIVITY_CACHE_FILE')

class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True
    TESTING = False

class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False
    TESTING = False

class TestingConfig(Config):
    """Testing configuration"""
    DEBUG = True
    TESTING = True

config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
