# Configuration

Each instance lives in a folder: `$DECAPOD_HOME`, `--dir PATH`, or the platform data directory under `instances/<name>`.

```
decapod.toml   settings (everything optional)
charter.toml   the charter (see charters.md)
.env           API keys and other secrets for this instance
decapod.db     SQLite: event log, cycles, agenda, memory, ledger, approvals
drive/         Decapod's private workspace (published/, sandbox/, .trash/)
logs/          daemon.log and decapod.jsonl
STOP           create this file to stop the daemon; `start` refuses while it exists
```

Settings are layered: built-in defaults, then `decapod.toml`, then environment variables of the form `DECAPOD_<SECTION>__<KEY>` (for example `DECAPOD_BUDGET__DAILY__USD=1.5`). Durations accept `"30s"`, `"5m"`, `"2h"` and `"1d"`. `decapod init` writes a commented `decapod.toml` showing the main options.

## Secrets

Secrets are read from three places, highest priority first:

1. the process environment
2. the instance `.env`
3. the user-wide `.env` (`decapod doctor` prints its path, for example `~/.config/decapod/.env`)

Put as many provider keys there as you like (see `.env.example`). Secrets are never included in prompts. They are redacted from tool output and from the event log.

## `[llm]`

| Key | Default | Meaning |
|---|---|---|
| `default_provider` | auto | The provider for tiers that do not name one. If unset, Decapod uses the first provider with a key, in this order: anthropic, openai, openrouter, deepseek, gemini, mistral, groq, together, xai, fireworks |
| `tiers` | preset | `FLASH`, `PRO`, `MAX` → `"provider:model"` or `"model"` |
| `providers.<name>` | presets | `base_url`, `api_key_env`, `requires_key`, `extra_headers`, `models = {FLASH=…, PRO=…, MAX=…}`. Overrides a preset, or defines a new endpoint |
| `fallbacks` | MAX→PRO→FLASH | The tier to try next when one fails after retries |
| `timeout` | `120s` | Per request |
| `max_retries` | 3 | Retries for retryable errors (429, 5xx, connection errors) |
| `max_concurrency` | 4 | Parallel requests |
| `min_request_interval` | `0s` | Pacing between requests, for strict rate limits |
| `json_mode` | `object` | `schema`, `object` or `none`. Falls back automatically if the provider refuses |
| `repair_attempts` | 2 | Retries with the validation error when structured output is invalid |
| `circuit_breaker_failures` / `circuit_breaker_cooldown` | 5 / `5m` | Stop calling a provider after this many failures, until the cooldown passes |
| `pricing` | `{}` | `"provider:model" = {input = 3.0, output = 15.0}` (USD per million tokens). Without it, USD budgets count as 0 |
| `embeddings_model` | none | `"provider:model"`. Enables vector memory search |

Synonyms are accepted for tier names: FAST, CHEAP and SMALL mean FLASH; STANDARD and SMART mean PRO; STRONG, BEST and FRONTIER mean MAX.

## `[schedule]`

| Key | Default | Meaning |
|---|---|---|
| `min_interval` | `30s` | Pause between cycles while it has momentum |
| `idle_interval` | `10m` | When it chooses to wait without saying how long |
| `max_interval` | `2h` | The longest sleep |
| `error_backoff` | `1m` | Doubles after each consecutive error, up to `max_interval` |

## `[budget.*]`

The windows are `cycle`, `hourly`, `daily` (local midnight) and `lifetime`. Each takes any of `usd`, `tokens`, `llm_requests`, `tool_calls` and `outbound`. Defaults:

- cycle: 40 requests, 400k tokens, 5 tool calls
- hourly: 300 requests
- daily: $2, 2000 requests, 10 outbound messages
- lifetime: $50

`warn_fraction` (0.8) sets when the agent is warned that a budget is running low.

## `[policy]`

| Key | Default | Meaning |
|---|---|---|
| `modes` | tier0–2 `auto`, tier3–4 `approval` | `auto`, `approval` or `deny` per risk tier (`tier3 = "auto"`) |
| `scope_modes` | `{}` | Per-scope overrides, using globs (`"github:me/notes:write" = "auto"`). When several match, the most restrictive wins |
| `rate_limits` | web.fetch 120/h, web.search 60/h | `"tool.glob" = {per_hour = N}` |
| `approval_ttl` | `24h` | Unanswered approvals expire, and are reported to the agent |
| `consecutive_denials_kill` | 8 | Stop the instance after this many policy denials in a row (0 = never) |

## `[caucus]`

| Key | Default | Meaning |
|---|---|---|
| `max_level` / `min_level` | `L3` / `L0` | Bounds on deliberation depth (`max_level = "L1"` is the cheapest setting that still debates) |
| `review_every` | 12 | A full agenda review (L2) every N cycles |
| `llm_triage` | true | A cheap model call that can raise the level |
| `escalate_on_tier` | tier2→L1, tier3→L3, tier4→L3 | The minimum level for an action of that tier |
| `disabled_roles` | `[]` | For example `["opportunist", "economist"]` |
| `role_tiers` | `{}` | For example `{critic = "PRO", executive = "MAX"}` |
| `role_weights` | `{}` | Ranking weight per role |
| `max_proposals_per_role` | 2 | |
| `loop_window` / `loop_threshold` | 6 / 3 | Loop detection |

## `[memory]`

`relevance_weight`, `recency_weight`, `importance_weight`, `goal_weight`, `recency_half_life` (`3d`), `retrieve_k` (8), `lessons_k` (5), `compact_every` (50 cycles), `compact_after` (`2d`), `context_tokens` (6000).

## `[tools]`

- `disabled = ["code.*"]`: disable tools by glob.
- `output_max_chars`: the most tool output kept (default 12000).
- `drive_max_file_bytes`: the largest file it may write (default 5 MB).
- `[tools.web]`: `search_provider` (`duckduckgo`, `searxng` or `brave`), `searxng_url`, `brave_api_key_env`, `user_agent`, `max_bytes`, `timeout`, `allow_private_addresses` (default false).
- `[tools.code]`: `isolation` (`auto`, `bwrap` or `none`), `timeout`, `memory_mb`, `allow_network`.
- `[tools.email]`: `enabled`, `address`, `imap_host`, `imap_port`, `smtp_host`, `smtp_port`, `username_env`, `password_env`, `mailbox`, `poll_interval`.
- `[tools.github]`: `enabled`, `token_env`, `api_url`.
- `[[tools.feeds]]`: `url`, `name`.
- `[tools.site]`: `build` (rebuild on every publish, default true), `title`, `description`, `base_url` (for absolute RSS links), `repo` (the GitHub Pages repository for `site.deploy`), `branch` (`gh-pages`), `path` (a folder inside the repo).
- `[tools.browser]`: `enabled`, `executable_path` (a Chromium binary, if Playwright's own isn't installed), `timeout` (`30s`).

## `[notify]`

| Key | Default | Meaning |
|---|---|---|
| `targets` | `[]` | `"ntfy:topic"`, an ntfy URL, a Slack or Discord webhook, or any URL that accepts a JSON POST. `${VAR}` is replaced from your secrets |
| `events` | approvals, stop/kill, budget exhausted, safety trip, cycle errors, reports, recurring goals due | Event kinds (globs) that trigger a notification |
| `min_interval` | `1m` | Don't repeat the same kind of notification more often than this. Approvals are exempt |

`decapod notify "hello"` sends a test message.

## `[report]`

`every` (for example `"7d"`; off by default): the daemon writes a progress report to `drive/reports/` and sends a notification. Run `decapod report --since 7d` for one on demand.

## `[[mcp.servers]]`

`name`, `command` (a list), `env`, `default_tier` (3), `tool_tiers`, `enabled`, `timeout`. See tools.md.
