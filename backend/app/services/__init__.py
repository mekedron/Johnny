"""Higher-level services that glue ORM state to runtime components.

Anything that needs SQLAlchemy plus a domain abstraction (e.g. persisting
voice-pipeline decisions to ``agent_decisions``, resolving a per-meeting
confidence threshold) lives here. The voice pipeline itself stays free of
ORM imports so the meet-worker image can ship without SQLAlchemy.
"""
