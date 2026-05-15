"""Studio dashboard — multi-page UI replacement for the legacy single-page dashboard.

Entry point: ``scripts/dashboard_server.py`` (thin shim) or
``scripts/dashboard/server.py`` (direct). Both run the same ``app.create_app()``.

Reads from existing data sources:
- ``registry/skills.json``      — skill catalog
- ``agents/<id>/AGENT.md``      — agent definitions
- ``commands/*.md``             — slash commands
- ``standards/decision-trees/`` — decision trees
- ``vector_index/lexical.sqlite`` — FTS5 skill search
- ``~/.claude/ka-sfskills/events.jsonl``    — live event log (tailed)
- ``~/.claude/ka-sfskills/skill_map.json``  — pre-built graph (lazy rebuild)
"""
