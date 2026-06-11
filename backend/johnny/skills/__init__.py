"""Skill packages + the tool layer for delegated tasks (Johnny-trt.23, Phase 4).

Johnny adopts openclaw's three-layer capability separation:

* **TOOLS execute** — :mod:`johnny.skills.tools` exposes the core
  ``sandbox.exec`` tool, which runs commands inside the ``skills-sandbox``
  compose container (Johnny-trt.35) and *only* there. The api / worker /
  agent-worker processes never execute skill commands themselves.
* **SKILLS instruct** — :mod:`johnny.skills.registry` discovers
  ``<skills-volume>/<name>/SKILL.md`` packages (openclaw / AgentSkills
  wire-compatible frontmatter, parsed by :mod:`johnny.skills.frontmatter`),
  gates eligibility on ``requires.bins`` resolved *inside* the sandbox, and
  feeds the router's task catalog (:mod:`johnny.agent.task_catalog`) plus the
  executor prompt.
* **MCP contributes tools** — Phase 6 (Johnny-trt.36) plugs ``mcp__*`` tools
  into the same :class:`~johnny.skills.tools.ToolRegistry`.

Execution policy v1 (:mod:`johnny.skills.policy`, openclaw ``DEFAULT_SAFE_BINS``
precedent): the guaranteed sandbox baseline toolset plus bins declared by
*eligible* skills are allowed; anything else is denied with an error naming
the binary. The allow set is computed by one function so the Phase-6
configurable policy engine (Johnny-trt.38) can layer on top without rework.

This ``__init__`` deliberately re-exports nothing: the registry / frontmatter
side is yaml+stdlib, while :mod:`johnny.skills.sandbox` / ``tools`` /
``executor`` pull ``httpx`` and ``app.providers.base`` — consumers import the
submodule they need so each import stays as cheap as it can be.
"""
