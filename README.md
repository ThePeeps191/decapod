<!-- # 🦀 Decapod
-->
<p align="center">
    <picture>
        <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/ThePeeps191/decapod/main/docs/assets/decapod-logo-text-dark.svg">
        <img src="https://raw.githubusercontent.com/ThePeeps191/decapod/main/docs/assets/decapod-logo-text.svg" alt="Decapod" width="500">
    </picture>
</p>

**Decapod** is an autonomous and self-proactive AI entity built with supervised multi-agent deliberation and advanced agentic capabilities. Instead of functioning as a personal AI assistant for a human, it works independently for and on behalf of itself and executes tasks based off of its own planning, agenda, and best interests. Its self-directed design allows it to run without needing any humans in-the-loop, though a TUI exists for monitoring and starting/stopping Decapod instances.

## Motivation

Current AI agents often revolve around the concept of personal AI assistants, where humans direct agents (primarily, as of July 2026, OpenClaw and Hermes Agent) to execute tasks and work for them. This may range from simple, guided tasks such as researching the internet to understand a topic to performing complex and continuous workflows such as autonomously building or reviewing software. Decapod is meant to be an AI agent that truly acts on its own (it works for itself, not a human), not one meant to help humans.

## Features

- **Multi-agent orchestration:** An master agent coordinates a caucus of specialist agents that propose, debate, critique, and rank actions before the supervisor commits to a decision. The inclusion of a committee-of-experts architecture allows for higher levels of reasoning and intelligence while lowering hallucinations and bad decisions.
- **Durable and persistent memory:** Decapod remembers all of its internal decisions/reasoning, goals, history, and objectives to ensure it works efficiently and effectively.
- **Tool and agentic integration:** Decapod is provided with numerous tools through hard-coded APIs and MCP servers which allow it to execute tasks ranging from searching the internet to deploying code to creating and sending emails.
- **Terminal monitor and kill switch:** Although, at its core, Decapod operates without any humans, a TUI exists for monitoring a live log of its actions, memory, goals, internal caucus, and decisions, and also gives users some control over Decapod such as a kill switch.

## Quick Start & Setup

First, clone this repository:

```bash
git clone https://github.com/ThePeeps191/decapod.git
```

Next, run the install wizard script located at `install.py` with:

```bash
cd decapod
python install.py
```

Alternatively, you may want to let an AI coding agent such as Claude Code, Codex, or OpenCode install Decapod for you. Simply paste the following instructions into your agent:

```text
Please view and follow the instructions at https://raw.githubusercontent.com/ThePeeps191/decapod/refs/heads/main/install.md to install Decapod.
```

## Tech Stack
Decapod was written entirely in Python with the following dependencies:

## Architecture (How It Works)

Decapod is built around the internal caucus, which is a committee-of-experts that are split up into various specialists that do everything from reflecting on past actions and what to improve on to designing goals for itself and planning how to execute them. Managers coordinate and summarize discussions/debates from the specialists, and an executive master agent decides based off of the managers' reports what Decapod should do next, while also organizing internal planning.

## License

Apache 2.0 - Danny Wang
