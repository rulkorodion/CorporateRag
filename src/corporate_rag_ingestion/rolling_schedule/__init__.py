"""Structured rolling-schedule ingestion and exact search.

Public classes are imported from their concrete modules. Keeping the package
initializer side-effect free avoids loading the FastAPI application from CLI
maintenance commands.
"""
