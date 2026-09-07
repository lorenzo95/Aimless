"""aimless — serverless chat with an AIM heart."""

__version__ = "0.7.6"

# Oldest daemon build this client fully supports. dist/ ships both together;
# an older daemon lacks the fixes this client depends on (attachment
# store-and-forward, non-blocking sends) and must be updated alongside the pyz.
MIN_DAEMON_BUILD = (0, 5, 2)
