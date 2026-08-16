"""TyranoScript/TyranoBuilder engine registration."""

from engines.base import registry
from engines.tyrano.engine import TyranoEngine

registry.register(TyranoEngine())

__all__ = ["TyranoEngine"]
