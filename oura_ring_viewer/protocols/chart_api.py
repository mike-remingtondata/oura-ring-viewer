"""Staff-authenticated SimpleAPI for the chart-drawer Application.

SCAFFOLD. Route shapes and auth model are settled; handler bodies are not
written yet. Modelled on ``dexcom-cgm-viewer/protocols/chart_api.py``, which
cleared Canvas review.

Routes:
  GET  /                       -> HTML shell (inline CSS+JS)
  GET  /data?patient_id=&range -> JSON view-model
  POST /sync?patient_id=&range -> manual incremental pull from Oura
  POST /send-link?patient_id=  -> mint magic-link JWT and deliver it
  POST /disconnect?patient_id= -> purge tokens and cached daily rows

Security invariants to hold while filling these in:
  * every route is staff-authenticated via ``StaffSessionAuthMixin``;
  * every handler re-checks that the caller may see ``patient_id``;
  * no route ever returns access or refresh token material.
"""

from canvas_sdk.effects import Effect
from canvas_sdk.effects.simple_api import HTMLResponse, JSONResponse, Response
from canvas_sdk.handlers.simple_api import SimpleAPI, StaffSessionAuthMixin, api


class OuraChartAPI(StaffSessionAuthMixin, SimpleAPI):
    """Staff-facing chart API. All routes require a Canvas staff session."""

    @api.get("/")
    def chart_shell(self) -> list[Response | Effect]:
        """Serve the chart HTML shell for the active patient."""
        raise NotImplementedError("TODO: render templates/chart.html")

    @api.get("/data")
    def chart_data(self) -> list[Response | Effect]:
        """Return the JSON view-model for the requested range."""
        raise NotImplementedError("TODO: build payload from OuraDaily rows")

    @api.post("/sync")
    def sync(self) -> list[Response | Effect]:
        """Pull new daily summaries from Oura on demand."""
        raise NotImplementedError("TODO: call services.sync.run_sync")

    @api.post("/send-link")
    def send_link(self) -> list[Response | Effect]:
        """Mint a magic-link JWT and deliver it to the patient."""
        raise NotImplementedError("TODO: mint JWT, deliver via portal or email")

    @api.post("/disconnect")
    def disconnect(self) -> list[Response | Effect]:
        """Purge stored tokens and every cached daily row for the patient."""
        raise NotImplementedError("TODO: delete tokens + OuraDaily rows")
