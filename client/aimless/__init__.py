"""aimless — serverless chat with an AIM heart."""

__version__ = "0.8.3"

# v2 is a clean break: client and daemon must match. The daemon refuses peers
# whose protocol version differs, and the client refuses an older daemon rather
# than half-parsing its replies.
PROTOCOL_VERSION = 2
MIN_DAEMON_BUILD = (0, 8, 0)
