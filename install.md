# Decapod installation instructions for coding agents

You are installing [Decapod](https://github.com/ThePeeps191/decapod) for your user. Decapod is an autonomous AI agent that runs as a background process and is monitored from a terminal UI.

Rules:

- If anything fails in a way that matters (permission errors, no disk space, a missing prerequisite, a failing command), **stop** and tell the user:
  - what failed and where,
  - the exact error,
  - how they can fix it, or how you could fix it with their permission.

  Harmless warnings, such as a deprecation notice, can be mentioned and ignored.
- Never invent or guess API keys. Ask the user for one, or let them enter it themselves in the interactive `decapod init`.
- Do not start Decapod (`decapod start`) unless the user asks. It is an autonomous agent that spends API credits.

## Prerequisites

- Python 3.11 or newer on `PATH`. Check with `python --version`; on some systems it is `python3`.
- `pip` (check `python -m pip --version`), or `uv`.
- Git, if installing from a clone.
- Optional, on Linux: `bubblewrap` (the `bwrap` command), which enables the sandboxed `code.run` tool.

Make sure you have permission to install packages and write files. If you don't, stop and tell the user how to grant it.

## Steps

1. Install the package. Prefer a virtual environment or `pipx`/`uv tool`:

   ```bash
   python -m pip install "git+https://github.com/ThePeeps191/decapod.git"
   ```

2. Check that it installed:

   ```bash
   decapod --version
   ```

3. Create an instance. Ask the user which charter they want (`decapod charters` lists them) and which model provider they use. If they give you a key:

   ```bash
   decapod init --yes --charter researcher --provider anthropic --api-key "<their key>"
   ```

   Otherwise, ask them to run `decapod init` themselves and enter the key at its prompt.

4. Verify:

   ```bash
   decapod doctor
   ```

   All model checks should pass. Warnings about disabled email, GitHub or bubblewrap are fine.

5. Tell the user:
   - where the instance lives (doctor prints it),
   - how to edit `charter.toml`,
   - how to start and watch Decapod: `decapod start`, then `decapod tui`,
   - how to stop it: `decapod stop`, or `decapod kill` for an immediate stop.
