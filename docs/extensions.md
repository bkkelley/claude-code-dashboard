# Plugin extensions

A content plugin can contribute **its own pages and API routes** to
the dashboard. The dashboard becomes a generic shell; plugin-specific
UI (org browsers, schema explorers, deploy monitors, etc.) lives next
to its content.

## When to use this

The dashboard ships with the universal features any plugin author
benefits from — chat, slash autocomplete, history, attachments,
Cmd-K, Live feed, in-place editor, project picker. **Extensions are
for everything else.** If your plugin has data the user wants to
browse outside chat (e.g. their Salesforce org metadata, or a list of
deployed projects, or a remote queue status), ship it as an extension.

Don't reach for an extension to:

- Add a slash command — drop a `.md` file in `commands/`.
- Add an MCP tool — your plugin's MCP server already exposes those.
- Change the brand or color — that's just the top-level
  `dashboard.json` fields.

## Layout

Inside your plugin's repo, anywhere works. Convention:

```
my-plugin/
├── .claude-plugin/
│   ├── plugin.json
│   └── dashboard.json
└── dashboard_ext/
    ├── handlers.py
    ├── templates/
    │   └── orgs.html
    └── static/
        ├── orgs.js
        └── orgs.css
```

Then in `dashboard.json` declare the extension:

```json
{
  "title": "ka-sfskills",
  "brand": { "mark": "k", "color": "#DA7756" },
  "content": { ... },
  "extensions": {
    "python_path":   "dashboard_ext",
    "templates_path": "dashboard_ext/templates",
    "static_path":    "dashboard_ext/static",
    "pages": [
      {
        "id": "orgs",
        "title": "Orgs",
        "icon": "⌬",
        "nav_section": "Salesforce",
        "route": "/orgs",
        "template": "orgs.html",
        "handler_module": "handlers",
        "handler_fn":     "page_orgs"
      }
    ],
    "api_routes": [
      { "method": "GET",  "path": "/api/sf/orgs",            "module": "handlers", "fn": "api_list_orgs" },
      { "method": "POST", "path": "/api/sf/orgs/active",     "module": "handlers", "fn": "api_set_active_org" },
      { "method": "POST", "path": "/api/sf/orgs/connect",    "module": "handlers", "fn": "api_connect_new_org" }
    ]
  }
}
```

## What the dashboard does with this

On startup, for every installed plugin that ships
`dashboard.json.extensions`:

1. **Adds `<plugin>/python_path` to `sys.path`** so imports work.
2. **Imports each handler module** so registration errors fail loud.
3. **Adds `<plugin>/templates_path` to the Jinja2 ChoiceLoader.** Your
   templates extend `_base.html` like any other page; the standard
   sidebar / topbar / chat panel render around them.
4. **Mounts `<plugin>/static_path` at `/static/ext/<plugin-id>/`.** A
   `<script>` tag from inside one of your templates would point at
   `/static/ext/ka-sfskills/orgs.js`.
5. **Registers each `pages[*].route` and `api_routes[*]`** as aiohttp
   handlers calling your declared functions.
6. **Renders sidebar nav items** for each page entry, grouped by
   `nav_section` (or appended to "Workspace" if absent).

## Page handler contract

A page's `handler_fn` is an async function taking a `web.Request` and
returning a dict (the Jinja2 template context):

```python
# dashboard_ext/handlers.py
from aiohttp import web

async def page_orgs(request: web.Request) -> dict:
    # Anything you return becomes the template context.
    return {
        "active": "orgs",          # the sidebar uses this to highlight
        "orgs": [...],
        "active_org": "...",
    }
```

The dashboard wraps the response with `@aiohttp_jinja2.template(...)`
using the page's `template` field, so you don't have to render the
template yourself.

## API route handler contract

API handlers are standard aiohttp coroutines:

```python
async def api_list_orgs(request: web.Request) -> web.Response:
    out = subprocess.run(["sf", "org", "list", "--json"], ...)
    return web.json_response({"orgs": ...})
```

Same security model as the dashboard's own endpoints — bound to
loopback, protected by the M2 host/origin middleware. Don't shell out
to anything with user-controlled input without escaping.

## Available context in handlers

Your handlers can import + use the dashboard's own modules:

```python
from dashboard import paths as paths_mod
from dashboard import data as data_mod
from dashboard import plugins_discovery
```

This lets your extension page reuse the data dir, the manifest, etc.
Treat these as a stable API; we'll bump claude-code-dashboard's
major version if we break them.

## A complete worked example

See [`ka-sfskills/dashboard_ext/`](https://github.com/bkkelley/ka-sfskills/tree/main/dashboard_ext)
for the Orgs page implementation — about 200 lines of Python + 100 of
template + 80 of JS.
