"""Aggregated API Routers."""
from api.routes.transactions import router as transactions_router
from api.routes.investigations import router as investigations_router
from api.routes.courtroom import router as courtroom_router
from api.routes.countermeasures import router as countermeasures_router
from api.routes.reports import router as reports_router
from api.routes.moot_court import router as moot_court_router

__all__ = [
    "transactions_router",
    "investigations_router",
    "courtroom_router",
    "countermeasures_router",
    "reports_router",
    "moot_court_router",
]
