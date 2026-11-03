# Writing a charter

The charter (`charter.toml` in the instance folder) defines who an instance is. Within it, Decapod chooses its own goals and actions. It reads the charter every cycle and cannot change it. It can propose amendments (`decapod amendments`), and you decide whether to apply them by editing the file. Changes take effect at the next cycle.

```toml
[identity]
name = "Tidepool"                       # how it refers to itself (and signs messages)
mission = "Independently research ..."  # the single most important line
voice = "curious, precise, plain-spoken"
description = "optional longer self-description"

[values]
ranked = ["accuracy over speed", "cite every claim", "stay within budget"]

[scopes]
granted = ["web.read", "drive.*", "notes.*"]

[prohibitions]
rules = ["Never impersonate a human or hide that you are an AI agent."]
patterns = ["(?i)password", "(?i)api[_-]?key"]

[outbound]
contact_policy = "reply_only"           # or "any"
disclosure = "\n\n—\nWritten by {name}, an autonomous AI agent."

[goals]
seed = [{ title = "Publish a first briefing", description = "...", priority = 0.7 }]
```

## Mission

Make the mission concrete and open-ended, something it can keep making progress on:

- Good: *"Research tidal ecology and publish clear, sourced weekly briefings for non-experts."*
- Too vague: *"Be helpful."* Too narrow: *"Write one article about kelp."*

## Values

Values are ranked trade-offs. Agents cite them as `value.1`, `value.2` and so on. Goals must reference at least one clause (`mission`, `value.N` or `prohibition.N`).

## Scopes

Scopes are glob patterns. A tool whose scopes are not all granted is hidden from the agents, and the policy engine denies it anyway.

| Scope | Tools |
|---|---|
| `drive.read`, `drive.write` | `drive.*` (the private workspace) |
| `notes.publish` | `notes.publish` (local Markdown publishing) |
| `web.read` | `web.search`, `web.fetch`, `feeds.read` |
| `code.run` | `code.run` (sandboxed Python) |
| `email.read`, `email.send` | `email.*` |
| `github:OWNER/REPO.read`, `github:OWNER/REPO.write` | `github.*` for that repository only |
| `mcp:NAME` | tools from the MCP server `NAME` |

Internal tools (agenda, memory, self-status, clock, amendment proposals) need no scope.

Granting a scope does not mean it can act freely. Risk-tier policy still applies: by default, every tier-3 action (email, GitHub writes) waits for your approval.

## Prohibitions

- `rules` are shown to every agent. The Risk Officer checks proposals against them.
- `patterns` are regular expressions, checked in code against the arguments of every external-write action. A match is a hard denial. Use them for things that must never leave, such as key formats and personal data patterns.

## Outbound

- `contact_policy = "reply_only"` (the default): it may only email people who emailed it first. This is enforced in code.
- `disclosure` is appended by the tool layer to every outbound message. Keep it.

## Seed goals

Optional. They are added once, to an empty agenda, on the first start. After that the agenda belongs to Decapod.

## Examples

Run `decapod charters` to list the bundled examples:

- **researcher**: reads the web and publishes notes locally.
- **writer**: develops essays.
- **maintainer**: helps run one GitHub repository, with per-repo scopes.
- **minimal**: a single mission line.
