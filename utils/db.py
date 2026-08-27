import os
from pymongo import MongoClient

_client = None

# Fallback connection string so the system works out of the box. Override it
# by setting a MONGO_URI environment variable on Render (recommended),
# without touching this file.
_DEFAULT_MONGO_URI = (
    "mongodb+srv://mdinabano189_db_user:OExxVWuoNAHuBlFr@cluster0.yzkibwa.mongodb.net/?appName=Cluster0"
)


def get_db():
    """
    MongoDB handle. Collections used:
      - lectures: { _id: name(slug), original_url, status, token,
                    video_filename, duration, file_size, created_at,
                    updated_at }
    """
    global _client
    mongo_uri = os.environ.get("MONGO_URI", _DEFAULT_MONGO_URI)

    if _client is None:
        _client = MongoClient(mongo_uri)

    db_name = os.environ.get("MONGO_DB_NAME", "pw_live_system")
    return _client[db_name]
