"""Johnny end-to-end smoke test (Johnny-f7k).

After ``.env`` is populated (FERNET_KEY, Google OAuth client, provider
API keys) this package runs a single command that takes the live ``.env``
and answers: "is this configuration actually usable, and where does it
break?"

Each check returns a :class:`SmokeResult` (PASS / SKIP / FAIL) with a
one-line reason. The CLI prints the rows in order and exits 0 only if
every non-SKIP check passed. SKIP is for optional providers with blank
keys; it never causes a non-zero exit.

Invoke as either::

    uv run johnny-smoke
    uv run python -m johnny.smoketest

The smoke test is intentionally narrower than the wizard: it does not
register providers, does not bring up the stack on its own (unless
``--start-stack`` is passed), and does not require user input. It is the
post-setup verification step the user runs after the wizard finishes
filling ``.env``.
"""

__all__: list[str] = []
