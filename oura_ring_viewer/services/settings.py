"""Plugin-wide constants and Oura environment routing.

VERIFY BEFORE FIRST LIVE CALL: the endpoint paths and scope strings below are
taken from the plugin scope document, not yet from a live Oura response. Check
them against https://cloud.ouraring.com/v2/docs once the API application is
registered, and correct here rather than at the call sites.
"""

DAILY_RETENTION_DAYS: int = 90
DEFAULT_RANGE_DAYS: int = 14
RANGE_OPTIONS: tuple[int, ...] = (7, 14, 30, 90)
MAGIC_LINK_TTL_SECONDS: int = 15 * 60

# Oura returns at most one summary row per day per family, so a 90 day window
# is 90 points per series. No downsampling is needed at v1 granularity.
MAX_CHART_POINTS: int = 90

# Oura hosted authorization lives on cloud.ouraring.com; the token exchange and
# all data reads live on api.ouraring.com.
OURA_AUTHORIZE_URL: str = "https://cloud.ouraring.com/oauth/authorize"
OURA_TOKEN_URL: str = "https://api.ouraring.com/oauth/token"
OURA_API_HOST: str = "https://api.ouraring.com"

# Space separated, per the OAuth2 spec. "personal" is required to resolve the
# Oura user id we store alongside the tokens.
OURA_OAUTH_SCOPE: str = "daily heartrate spo2 workout heart_health personal"

# Endpoint suffixes, joined onto the environment base path.
ENDPOINT_DAILY_SLEEP: str = "daily_sleep"
ENDPOINT_DAILY_READINESS: str = "daily_readiness"
ENDPOINT_DAILY_ACTIVITY: str = "daily_activity"
ENDPOINT_DAILY_SPO2: str = "daily_spo2"
ENDPOINT_PERSONAL_INFO: str = "personal_info"

REQUIRED_SECRETS: tuple[str, ...] = (
    "OURA_CLIENT_ID",
    "OURA_CLIENT_SECRET",
    "OURA_REDIRECT_URI",
    "OURA_ENVIRONMENT",
    "OURA_MAGIC_LINK_SECRET",
)


def oura_collection_base(environment: str) -> str:
    """Return the usercollection base URL for the given environment string.

    Oura's sandbox is a path prefix on the same host, not a separate host, and
    it serves the same response shapes as production. That is what lets this
    plugin be built and reviewed without a physical ring.
    """
    env = (environment or "").strip().lower()
    if env == "production":
        return f"{OURA_API_HOST}/v2/usercollection"
    return f"{OURA_API_HOST}/v2/sandbox/usercollection"


def parse_range_days(raw: str | None) -> int:
    """Coerce a range query parameter into an allowed day count."""
    if not raw:
        return DEFAULT_RANGE_DAYS
    cleaned = raw.strip().lower().rstrip("d")
    try:
        days = int(cleaned)
    except (TypeError, ValueError):
        return DEFAULT_RANGE_DAYS
    if days not in RANGE_OPTIONS:
        return DEFAULT_RANGE_DAYS
    return days
