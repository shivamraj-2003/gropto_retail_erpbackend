"""Import every model module so Base.metadata is complete for Alembic autogenerate."""

from app.models import models, models_phase2, models_phase3  # noqa: F401
