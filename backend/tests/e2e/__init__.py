"""End-to-end test harnesses for Johnny.

These harnesses talk to a live Compose stack and (for ``providers_ui``)
to a real browser via the chrome-devtools-mcp tool surface. They are
opt-in: ``pytest -m e2e_ui`` selects them and they are skipped from the
default ``pytest`` run so unit tests stay self-contained.
"""
