"""Krabs web dashboard (stdlib only)."""

from .data import DashboardData
from .server import DashboardHTTPServer, serve

__all__ = ["DashboardData", "DashboardHTTPServer", "serve"]
