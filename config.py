import os
from dotenv import load_dotenv
load_dotenv()

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY","dev")
    DEBUG = os.environ.get("FLASK_DEBUG","false").lower()=="true"
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY","")
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL","gemini-2.5-flash-preview-05-20")
    NEWS_API_KEY = os.environ.get("NEWS_API_KEY","")