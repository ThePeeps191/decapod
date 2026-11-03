# Tools

Every tool declares:

- a **risk tier**: 0 internal, 1 read, 2 local write, 3 external write, 4 irreversible
- **scopes**, which the charter must grant
- typed arguments (Pydantic)

The policy engine decides whether each call runs, needs approval or is denied. `decapod doctor` lists which tools are available and why the others are not.

## Built-in tools

| Tool | Tier | Scope | Notes |
|---|---|---|---|
| `agenda.add_goal`, `agenda.update_goal`, `agenda.plan`, `agenda.update_task` | 0 | – | Goals must cite charter clauses; plans are versioned DAGs |
| `memory.remember`, `memory.search` | 0 | – | Semantic facts, plus search across all memory |
| `self.status`, `clock.now` | 0 | – | |
| `charter.propose_amendment` | 0 | – | Recorded for the operator; it cannot edit the charter |
| `drive.list`, `drive.read` | 0 | `drive.read` | Confined to the drive folder |
| `notes.index` | 0 | `drive.read` | |
| `drive.write`, `drive.delete` | 2 | `drive.write` | Delete moves files to `.trash` |
| `notes.publish` | 2 | `notes.publish` | Markdown with front matter, plus an index, in `drive/published/` |
| `web.search` | 1 | `web.read` | DuckDuckGo (default), SearXNG or Brave |
| `web.fetch` | 1 | `web.read` | Readable text, size limit, SSRF checks on every redirect |
| `feeds.read` | 1 | `web.read` | RSS and Atom; entity declarations refused |
| `code.run` | 2 | `code.run` | bubblewrap sandbox (see `docs/safety.md`) |
| `email.list`, `email.read` | 1 | `email.read` | IMAP; needs `[tools.email]` and credentials |
| `email.reply`, `email.send` | 3 | `email.send` | SMTP, disclosure footer, reply-only policy |
| `github.list_issues`, `github.read_issue`, `github.read_file` | 1 | `github:OWNER/REPO.read` | Needs `[tools.github]` and `GITHUB_TOKEN` |
| `github.create_issue`, `github.comment`, `github.write_file` | 3 | `github:OWNER/REPO.write` | Disclosure footer, plus a note in the commit message |

Anything a tool returns from the outside world is marked as untrusted content.

## Sensors

Some things reach Decapod as percepts, without it having to act:

- New items from `[[tools.feeds]]` (checked every 30 minutes).
- New email (every `tools.email.poll_interval`).
- Operator notes (`decapod note`).
- Approval outcomes.
- Budget warnings.

## MCP servers

```toml
[[mcp.servers]]
name = "fetch"
command = ["uvx", "mcp-server-fetch"]
default_tier = 1                       # tier for this server's tools (default 3)
tool_tiers = { dangerous_tool = 4 }    # per-tool overrides
env = { SOME_SETTING = "x" }
```

Tools are imported as `mcp.<server>.<tool>` with the scope `mcp:<server>`, which the charter must grant. Their descriptions come from the server and are labeled untrusted. Servers are started with the daemon and killed with it.

## Writing your own tool

```python
from decapod.domain import RiskTier, SideEffect, ToolResult
from decapod.tools import Tool, ToolArgs


class WeatherArgs(ToolArgs):
    city: str


class Weather(Tool):
    name = "weather.now"
    description = "Current weather for a city."
    Args = WeatherArgs
    tier = RiskTier.READ
    side_effect = SideEffect.EXTERNAL_READ
    required_scopes = ("web.read",)

    async def run(self, args: WeatherArgs, ctx) -> ToolResult:
        response = await ctx.http.get("https://wttr.in/" + args.city, params={"format": 3})
        return ToolResult.success(response.text)
```

To make the daemon load your tools, publish them as a plugin. In your package's `pyproject.toml`:

```toml
[project.entry-points."decapod.tools"]
weather = "my_package.tools:Weather"   # a Tool class, a Tool instance, or a function returning a list
```

Install the package in the same environment as Decapod, and `decapod doctor` will list the tool. In Python you can also pass tools directly: `Instance(config, extra_tools=[Weather()])`. Any tool still needs its scopes granted by the charter.

Guidelines:

- Declare the lowest tier that is honest about the tool's effect.
- Use `precheck()` for checks that depend on instance state. A message returned there becomes a policy denial.
- Set `outbound = True` for anything that sends messages. Then the tool counts against the outbound budget and is refused if its arguments contain a secret.
