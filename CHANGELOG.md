# Changelog

## 1.1.0

- **Recurring goals and deadlines.**
  - Goals take `every` ("7d") and `due` ("2026-12-01" or "3d").
  - A completed recurring goal reopens with a fresh plan when it comes due.
  - Overdue goals are flagged once.
  - Seed goals in the charter can recur.
  - Existing databases upgrade in place.
- **A website and RSS feed.**
  - Published notes become `drive/site/`, which is rebuilt automatically.
  - `site.deploy` pushes the site to GitHub Pages in a single commit (tier 3).
- **`browser.read`.**
  - Headless Chromium for JavaScript-heavy pages, with optional screenshots (`decapod[browser]`).
  - Every request a page makes is address-checked.
- **Notifications** through ntfy, Slack, Discord or any webhook, for approvals, stops and kills, exhausted budgets, safety trips, errors, reports and recurring goals coming due.
- **Progress reports**:
  - `decapod report --since 7d` builds one on demand.
  - `[report] every` makes the daemon write one on a schedule.
  - Reports use no model calls.
- **`decapod web`** serves the monitor in a browser (`decapod[web]`).

## 1.0.0

The first complete release. Decapod was rebuilt from the early prototype into a full system:

- **Cognitive loop and daemon**: perceive → triage → deliberate → act → reflect. The daemon adapts its cadence, writes a heartbeat, handles signals, recovers from crashes, and has a PID file.
- **Caucus**: a six-round deliberation (propose, critique, revise, rank, synthesize, decide) across strategy, operations and oversight chambers, with L0–L3 escalation, Borda ranking, recorded dissent and loop detection.
- **Memory**: an append-only event log, a goal and task agenda with versioned plans, and episodic, semantic and reflective memory with scored retrieval and compaction.
- **Safety walls**: risk tiers, charter scopes and prohibition patterns, hierarchical budgets, an approval queue, a kill switch (TUI, CLI, STOP file, signals), secret redaction, SSRF protection and disclosure footers.
- **Tools**: web search and fetch, feeds, drive, Markdown publishing, sandboxed Python, email, GitHub, MCP servers, and plugins via the `decapod.tools` entry point.
- **Models**: any OpenAI-compatible provider. Presets choose a provider from the API keys in `.env` files, and there are three tiers (FLASH, PRO, MAX). Calls get retries, fallbacks, a circuit breaker, structured output with repair, and metering.
- **Interfaces**: a CLI (`init`, `doctor`, `start`, `stop`, `kill`, `status`, `log`, `show`, `replay`, `note`, `approvals`, `eval`, …) and a Textual monitor.
- **Quality**: a deterministic replay of recorded cycles, scenario evals, a scripted offline brain, and about 190 tests that run fully offline.
