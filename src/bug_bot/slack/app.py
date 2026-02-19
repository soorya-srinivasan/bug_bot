from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

from bug_bot.config import settings

slack_app = AsyncApp(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
)

# HTTP mode handler (used when SLACK_SOCKET_MODE=false)
slack_handler = AsyncSlackRequestHandler(slack_app)
