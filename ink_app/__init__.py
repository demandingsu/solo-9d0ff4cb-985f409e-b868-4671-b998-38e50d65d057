"""专色墨称量核定 Flask 应用包。"""

from .app import create_app
from . import domain

__all__ = ["create_app", "domain"]
