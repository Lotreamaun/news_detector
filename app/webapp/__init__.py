"""Пакет WebApp (Mini-App) для просмотра полного текста закона."""

from app.webapp.server import create_app, start_webapp, stop_webapp

__all__ = ["create_app", "start_webapp", "stop_webapp"]
