"""Initial control-plane schema."""

from app.db import Base
from app.models.runtime import Deployment, DeploymentEvent, ManagedUnit, UnitHealthSnapshot  # noqa: F401

VERSION = "001_initial"


def upgrade(engine) -> None:
    """Create the initial schema."""

    Base.metadata.create_all(bind=engine)
