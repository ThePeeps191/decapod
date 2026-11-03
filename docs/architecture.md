# Architecture

Decapod is one Python package (`src/decapod`) organized in layers. A layer only imports layers below it.

```
cli/  tui/                      human surface: commands and the Textual monitor
daemon/                         process lifecycle: run loop, scheduler, PID file, signals
runtime/                        the cognitive loop, perception, wiring, replay
caucus/  executive/  agents/    cognition: deliberation, reflection, the roster
prompts/                        versioned Jinja templates
tools/                          effectors: registry, executor, built-ins, sandbox, MCP
memory/                         agenda, long-term memory, context budgeting
control/                        hard walls: budget, policy, approvals, kill switch
llm/                            providers, routing, retries, structured output, metering
storage/  logs/                 SQLite, event log, event bus, logging
config/                         settings, charter, providers, paths, secrets
domain/  util/                  shared models, enums, errors and helpers
evals/                          scripted brain and scenario evals
```

## Processes

```
decapod start ─► daemon process (asyncio)
                 ├─ run loop: one cycle at a time, never overlapping
                 ├─ control watcher: polls control commands every 0.5 s (a kill lands mid-call)
                 ├─ heartbeat: writes a timestamp every 2 s
                 └─ child processes: sandboxed code, MCP servers (killed with the daemon)

decapod tui ───► separate process: reads the database, writes only control commands,
                 operator notes and approval decisions
decapod kill ──► kill command → SIGTERM after 5 s → SIGKILL after 10 s more
```

All state lives in one SQLite file per instance (WAL mode, so readers never block the writer). The TUI and CLI open their own connections. If the TUI crashes, the agent is unaffected. If the agent crashes, its last cycle is marked *aborted* at the next start, and the agent is told about it as a percept.

## One cycle

`runtime/loop.py`:

1. **Budget pre-check.** If any LLM budget window is exhausted, sleep until it resets. If the *lifetime* budget is exhausted, stop.
2. **Begin.** Record the cycle (`c00042`). Reload the charter if it changed. Log its SHA-256.
3. **Execute approved actions.** Run anything the operator approved since the last cycle. Each one goes through the policy engine again, with `preapproved=True`, and has an idempotency key.
4. **Perceive** (`runtime/perceive.py`): operator notes, approval outcomes, budget warnings, crash notices, denial streaks, new feed items and new email.
5. **Build the working context** (`caucus/context.py`):
   - the focus goal and task, and the agenda
   - percepts and the recent cycles
   - relevant memories and lessons
   - the budget
   - planner notes and loop warnings
   - the scope-filtered tool catalog

   The whole context is saved in a `cycle.context` event so the cycle can be replayed.
6. **Triage** (`caucus/escalation.py`). Rules pick L0–L3. An optional cheap model call can only raise the level.
7. **Deliberate** (`caucus/protocol.py`). This produces exactly one `Decision`.
8. **Act.** The registry builds the policy's view of the action. Then `PolicyEngine.review()` returns one of three outcomes:
   - *allow*: the executor runs the tool.
   - *approve*: the action is queued for the operator.
   - *deny*: the denial is reported back as a percept.
9. **Reflect** (`executive/reflector.py`):
   - write an episodic memory and apply task and goal progress
   - store facts, plus lessons (deduplicated)
   - score lessons and recorded dissent
   - compact old memories every N cycles
10. **Finish.** Record the decision, outcome and cost. The scheduler picks the sleep: momentum, the executive's own `wait`, error backoff, or a budget reset.

A checkpoint runs between phases. It blocks while paused and raises `Halted` when stopping or killed.

## The caucus

| Round | L0 | L1 | L2 | L3 |
|---|---|---|---|---|
| 1. Propose | planner | strategist, planner, operator | + opportunist, researcher | same |
| 2. Critique | – | critic | economist, critic, risk officer, historian | same |
| 3. Revise | – | – | authors of contested proposals | + second critique round |
| 4. Rank | – | critic scores | proposers score others' proposals | same |
| 5. Synthesize | – | – | strategy, operations and oversight managers | same |
| 6. Decide | executive confirms (FLASH) | executive (PRO) | executive (PRO) | executive (MAX) |

The rules of the debate:

- Every proposal is **admission-checked** against the tool registry and policy before it is debated, so out-of-bounds ideas cost nothing more.
- When two specialists propose the same action, the proposals are merged.
- Critics see proposals anonymously.
- Rankings use a weighted Borda count, and proposals flagged for a charter concern are penalized.
- If the chosen action's risk tier requires a higher level (by default tier 2 needs L1 and tier 3+ needs L3), the caucus deliberates again at that level.
- The executive may always choose to `wait`.

## Memory

| Store | What | Retrieval |
|---|---|---|
| Event log | Everything, append-only | By cycle, kind or id |
| Agenda | Goals and versioned task plans (DAGs) | Structured |
| Episodic | One summary per cycle; old ones are merged into daily digests | Relevance + recency + importance + goal |
| Semantic | Facts it learned, with confidence | Same, plus optional embeddings |
| Reflective | Lessons, with a usefulness score that rises when they help | Relevance + usefulness |

Text search uses SQLite FTS5, falling back to `LIKE` where FTS5 is missing. Embeddings are optional (`llm.embeddings_model`). Prompts are assembled by a token-budgeted context builder: low-priority sections shrink first, so a context overflow is never an error.

## Models

`config/providers.py` merges built-in presets with your `[llm.providers.*]` and resolves each tier to a `provider:model` route.

`llm/service.py` adds the call handling:

- retries with backoff and jitter
- per-tier fallbacks: MAX → PRO → FLASH
- a circuit breaker that stops calling a provider after repeated failures, until a cooldown passes
- request pacing and a concurrency cap
- budget pre-authorization and settlement
- per-call cost metering

Every call is recorded in `llm_calls`, which is what makes replay possible. Structured output is validated with Pydantic and repaired automatically. If a provider rejects `response_format`, Decapod falls back to instructions in the prompt.

## Replay and evals

- `decapod replay c00042` restores the recorded working context and answers every model call from `llm_calls`, matched by purpose and order. It reports whether the same decision comes out.
- `decapod eval` builds throwaway instances with a fake web and a decoy `email.send` tool, runs the real daemon and checks the outcome in the database. It runs offline with the scripted brain (`evals/brain.py`), or with your models via `--real`.
