"""Patient-facing OAuth bridge: ``/connect`` and ``/callback``.

SCAFFOLD. Handler bodies are not written yet.

These routes intentionally bypass ``StaffSessionAuthMixin`` because the patient
following the link is not authenticated to Canvas. Security comes from:

  * the magic-link JWT (HMAC-SHA256, short expiry, single-use nonce); and
  * the OAuth ``state`` parameter on the callback (signed patient_id + nonce).

``authenticate`` always returns True; per-route validation runs inside each
handler. This is the idiomatic way to expose an unauthenticated public callback
under the Canvas SimpleAPI auth model, and is what ``dexcom-cgm-viewer`` does.

Do not add a route to this class that is not itself independently validated.
"""

from canvas_sdk.effects import Effect
from canvas_sdk.effects.simple_api import HTMLResponse, Response
from canvas_sdk.handlers.simple_api import SimpleAPI, api
from canvas_sdk.handlers.simple_api.security import Credentials


class OuraOAuthAPI(SimpleAPI):
    """Patient-facing OAuth bridge. Auth happens per-handler via JWT/state."""

    def authenticate(self, credentials: Credentials) -> bool:
        """Always accept; each handler validates its own token."""
        return True

    @api.get("/connect")
    def connect(self) -> list[Response | Effect]:
        """Verify the magic link, then redirect to Oura's hosted authorize page."""
        raise NotImplementedError(
            "TODO: verify magic-link JWT, burn nonce, build authorize URL "
            "with signed state, redirect to settings.OURA_AUTHORIZE_URL"
        )

    @api.get("/callback")
    def callback(self) -> list[Response | Effect]:
        """Exchange the authorization code for tokens and persist them."""
        raise NotImplementedError(
            "TODO: verify state, exchange code at settings.OURA_TOKEN_URL, "
            "persist via services.oauth.persist_tokens, render success page"
        )
