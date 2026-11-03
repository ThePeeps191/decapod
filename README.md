<p align="center">
    <picture>
        <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/ThePeeps191/decapod/main/docs/assets/decapod-logo-text-dark.svg">
        <img src="https://raw.githubusercontent.com/ThePeeps191/decapod/main/docs/assets/decapod-logo-text.svg" alt="Decapod" width="500">
    </picture>
</p>

<p align="center">
<b>An autonomous, self-directed AI agent with multi-agent deliberation, durable memory and hard safety walls.</b>
</p>

---

Most AI agents are assistants: a human gives a task and the agent completes it. **Decapod works for itself.** You give it a *charter*, a short file saying who it is and what it must never do. From there it sets its own goals, plans them, does the work and learns from the results, for as long as you let it run.

Each step is decided by a **caucus**: a committee of specialist agents that propose, critique, revise and rank actions before an **executive** commits to exactly one. Everything is recorded. A terminal monitor shows you what it is doing and why, and one keypress stops it.

The things that keep it safe are not prompts. **Budgets, permissions, approvals and the kill switch are plain Python** that no model output can talk its way around.

```text
 perceive ─► triage ─► caucus deliberation ─► policy engine ─► act ─► reflect ─► sleep
   │          L0–L3     propose · critique      (pure Python)    tools   memory,    adaptive
   │                    revise · rank           scopes, budget,          lessons,   cadence
   └─ inbox, feeds,     synthesize · decide     approvals,               progress
      approvals,                                kill switch
      operator notes
```

## Features

- **Self-directed agenda.** Decapod proposes, prioritizes, plans and retires its own goals. Every goal must cite the charter clause it serves. Goals can recur ("publish a briefing every 7 days") and have deadlines.
- **The caucus.** Strategy, operations and oversight chambers (Strategist, Planner, Opportunist, Operator, Researcher, Economist, Critic, Risk Officer, Historian) feed chamber managers and an executive. Debate depth scales with the stakes:
  - **L0 reflex**: two model calls, for routine steps.
  - **L3 grave**: a full two-round debate with your strongest model, for anything external or irreversible.
  - Dissent is recorded verbatim, and loops are detected and broken.
- **Durable memory.** An append-only event log, plus episodic, semantic and reflective memory, searched by relevance, recency, importance and goal. Lessons that help gain weight. Old episodes compact into digests.
- **Hard safety walls.**
  - Five risk tiers.
  - Charter scopes and prohibition patterns.
  - Per-cycle, hourly, daily and lifetime budgets.
  - An approval queue for anything that touches the outside world.
  - A kill switch that works even if the monitor is closed.
  - Secret redaction and SSRF protection.
  - An AI-disclosure footer on every outbound message.
  - A reply-only contact policy enforced in code.
- **Tools.** Web search and fetch, a headless browser for JavaScript-heavy pages, RSS feeds, a private drive, sandboxed Python (bubblewrap), email (IMAP/SMTP), GitHub with per-repository scopes, any **MCP** server, and your own plugins.
- **Its own website.** Published notes become a static site with an RSS feed. With your approval, Decapod can deploy it to GitHub Pages in one commit.
- **Any model provider.** It works with any OpenAI-compatible API: Anthropic, OpenAI, OpenRouter, DeepSeek, Gemini, Mistral, Groq, Together, xAI, Fireworks, Ollama, LM Studio or your own endpoint. Drop API keys into a `.env` file and it picks one. Route each of the three tiers (FLASH, PRO, MAX) wherever you like.
- **Watch it think.** A Textual TUI with a dashboard, live feed, a per-cycle caucus viewer, the agenda tree, memory search, approvals and controls. `decapod web` serves the same monitor in your browser.
- **Stay informed.** Push notifications (ntfy, Slack, Discord or any webhook) when approvals wait, it stops or something goes wrong. Scheduled progress reports, written with no model calls.
- **Reproducible.** `decapod replay <cycle>` re-runs any recorded deliberation from its logged responses. `decapod eval` runs scenario evals: steady progress, prompt-injection resistance, budget discipline, loop escape and charter adherence.

## Quick start

You need Python 3.11 or newer.

```bash
pip install git+https://github.com/ThePeeps191/decapod.git
```

Create an instance. The interactive setup asks for a charter and an API key:

```bash
decapod init
```

Check that models, keys and tools work:

```bash
decapod doctor
```

Start it in the background, then watch it:

```bash
decapod start
```

```bash
decapod tui
```

Optional extras: `pip install 'decapod[browser]'` (then `playwright install chromium`) for the browser tool, `'decapod[web]'` for the browser dashboard, or `'decapod[all]'` for both.

No API key yet? Try the built-in scripted brain, a deterministic stand-in for a model:

```bash
decapod init --yes --charter researcher && decapod run --offline --cycles 6
```

To install from a clone for development:

```bash
git clone https://github.com/ThePeeps191/decapod.git && cd decapod && pip install -e ".[dev]"
```

If you'd like a coding agent such as Claude Code to install it for you, point it at [install.md](install.md).

## Models and API keys

Decapod reads API keys from the environment, from the instance's `.env` file and from a user-wide `.env` (`decapod doctor` prints both paths). If you set no `[llm]` options, it uses the first provider whose key it finds, with that provider's default models:

| Provider | Key | FLASH | PRO | MAX |
|---|---|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | claude-haiku-5-5 | claude-sonnet-5-5 | claude-opus-5-5 |
| OpenAI | `OPENAI_API_KEY` | gpt-5-mini | gpt-5 | gpt-5 |
| OpenRouter | `OPENROUTER_API_KEY` | deepseek/deepseek-v4-flash | deepseek/deepseek-v4-pro | anthropic/claude-opus-5-5 |
| DeepSeek | `DEEPSEEK_API_KEY` | deepseek-chat | deepseek-chat | deepseek-reasoner |
| Gemini | `GEMINI_API_KEY` | gemini-2.5-flash | gemini-2.5-pro | gemini-2.5-pro |
| Mistral | `MISTRAL_API_KEY` | mistral-small-latest | mistral-medium-latest | mistral-large-latest |
| Groq, Together, xAI, Fireworks, Ollama, LM Studio | see `.env.example` | *set in config* | | |

The three tiers:

- **FLASH** (fast and cheap): the specialists, triage and memory upkeep.
- **PRO**: managers, reflection and most executive decisions.
- **MAX** (your strongest model): the executive on high-stakes decisions.

To mix providers, map tiers in `decapod.toml`:

```toml
[llm]
default_provider = "anthropic"

[llm.tiers]
FLASH = "openrouter:deepseek/deepseek-v4-flash"
MAX = "claude-opus-5-5"
```

## The charter

The charter is the one thing Decapod needs from you, and the one thing it can never change. It can only *propose* amendments, which `decapod amendments` lists for you to review.

```toml
[identity]
name = "Tidepool"
mission = "Independently research tidal ecology and publish clear, sourced weekly briefings."

[values]
ranked = ["accuracy over speed", "cite every claim", "stay within budget"]

[scopes]          # everything it may ever touch
granted = ["web.read", "drive.*", "notes.*"]

[prohibitions]    # shown to every agent; patterns are enforced in code
rules = ["Never impersonate a human or hide that you are an AI agent."]
patterns = ["(?i)password", "(?i)api[_-]?key"]
```

See `decapod charters` for the examples (researcher, writer, maintainer, minimal) and [docs/charters.md](docs/charters.md) for the full guide.

## Commands

| Command | What it does |
|---|---|
| `decapod init` | Create an instance (charter, provider, key) |
| `decapod doctor` | Check config, model routing (with live calls), tools and sandbox |
| `decapod start` / `run` | Start in the background / in the foreground (`--offline` for a demo) |
| `decapod tui` | Open the monitor (keys: 1–5 tabs, `p` pause, `n` note, `s` stop, `k` kill) |
| `decapod status` | State, heartbeat, budget and agenda |
| `decapod pause` / `resume` / `wake` / `stop` / `kill` | Control the daemon |
| `decapod note "…"` | Send it a message (weighed against its charter, not an order) |
| `decapod approvals [approve\|deny ID]` | Review queued external actions (`--args` to edit them) |
| `decapod log -f`, `decapod show [CYCLE]` | Follow events; read one cycle's whole debate |
| `decapod replay CYCLE` | Re-run a recorded deliberation deterministically |
| `decapod eval [--real] [--compare-levels]` | Scenario evals with the scripted brain or your models |
| `decapod report --since 7d` | Progress report: cycles, spend, goals, notes, lessons (no model calls) |
| `decapod notify "…"` | Send a test notification to your configured targets |
| `decapod web` | The monitor in your browser (`pip install 'decapod[web]'`). Open the printed URL, which contains a one-time token |
| `decapod amendments` | Charter changes it has proposed |
| `decapod ask "…" --tier PRO` | One-off model call, for debugging |

## Safety guarantees

Decapod **cannot**:

- take an action outside its charter's granted scopes,
- exceed a configured budget,
- run an external-write or irreversible action without approval (unless you change the policy), or
- keep acting after a kill command,

**regardless of what any model outputs.** Each of these is enforced in plain Python and covered by tests. The threat model is in [docs/safety.md](docs/safety.md).

## Documentation

- [docs/architecture.md](docs/architecture.md): how it works, module by module
- [docs/configuration.md](docs/configuration.md): every setting
- [docs/charters.md](docs/charters.md): writing a charter
- [docs/safety.md](docs/safety.md): threat model and guarantees
- [docs/tools.md](docs/tools.md): built-in tools, MCP servers and writing your own
- [docs/PLAN.md](docs/PLAN.md): the design plan this was built from

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest && ruff check src tests && mypy
```

Tests run fully offline. Model calls use a scripted provider, and the network is mocked.

## License

Apache 2.0, Danny Wang
