"""`decapod init`: create an instance (config, charter, API keys) and check it."""

from __future__ import annotations

import os
import sys
from importlib import resources
from pathlib import Path

from rich.prompt import Confirm, Prompt

from decapod.cli.output import console, err
from decapod.config import InstancePaths, Secrets
from decapod.config.providers import AUTO_ORDER, PRESETS, has_key, provider_configs
from decapod.config.secrets import user_env_file
from decapod.config.settings import LLMSettings
from decapod.domain import Tier


def example_charters() -> dict[str, str]:
    folder = resources.files("decapod.examples").joinpath("charters")
    return {
        entry.name.removesuffix(".toml"): entry.read_text(encoding="utf-8")
        for entry in sorted(folder.iterdir(), key=lambda e: e.name)
        if entry.name.endswith(".toml")
    }


def config_template() -> str:
    return resources.files("decapod.examples").joinpath("decapod.toml").read_text(encoding="utf-8")


def charter_summary(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("mission"):
            return line.split("=", 1)[1].strip().strip('"')[:100]
    return ""


def render_config(name: str, provider: str | None, models: dict[Tier, str] | None) -> str:
    template = config_template()
    if provider:
        provider_line = f'default_provider = "{provider}"'
    else:
        provider_line = (
            '# default_provider = "anthropic"   # unset: the first provider whose API key is found'
        )
    tier_lines = []
    preset = PRESETS.get(provider or "")
    for tier in Tier:
        model = (models or {}).get(tier)
        if model:
            tier_lines.append(f'{tier.value} = "{model}"')
        else:
            default = preset.models.get(tier) if preset else None
            hint = f'"{default}"' if default else '"<provider>:<model>"'
            tier_lines.append(f"# {tier.value} = {hint}")
    return (
        template.replace("@@NAME@@", name)
        .replace("@@PROVIDER@@", provider_line)
        .replace("@@TIERS@@", "\n".join(tier_lines))
    )


def write_env(path: Path, values: dict[str, str]) -> None:
    lines: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip()
            if "=" in line and not line.lstrip().startswith("#") and key in values:
                continue  # replaced below
            lines.append(line)
    for key, value in values.items():
        lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def run_init(
    root: Path | None,
    instance: str | None,
    *,
    charter: str | None,
    name: str | None,
    provider: str | None,
    api_key: str | None,
    models: dict[Tier, str],
    yes: bool,
    force: bool,
    user_wide_key: bool,
) -> int:
    paths = InstancePaths.resolve(root, instance)
    interactive = not yes and sys.stdin.isatty()
    if paths.exists() and not force:
        err.print(
            f"[red]An instance already exists at {paths.root}[/] (use --force to "
            "overwrite its config and charter; memory and history are kept)."
        )
        return 1
    paths.ensure()
    console.print(f"[bold]Creating a Decapod instance[/] in {paths.root}\n")

    # ------------------------------------------------------------- charter
    examples = example_charters()
    charter_text: str
    if charter and Path(charter).expanduser().exists():
        charter_text = Path(charter).expanduser().read_text(encoding="utf-8")
    else:
        choice = charter
        if choice is None and interactive:
            console.print("[bold]Charter[/] (who this agent is and what it must never do):")
            for key, text in examples.items():
                console.print(f"  [cyan]{key:<11}[/] {charter_summary(text)}")
            choice = Prompt.ask("Start from", choices=list(examples), default="researcher")
        choice = choice or "researcher"
        if choice not in examples:
            err.print(
                f"[red]Unknown charter {choice!r}[/] (examples: {', '.join(examples)}; "
                "or pass a path to a .toml file)"
            )
            return 1
        charter_text = examples[choice]
    if name:
        charter_text = _set_name(charter_text, name)

    # ------------------------------------------------------------- models / keys
    secrets = Secrets.load(paths.env)
    configs = provider_configs(LLMSettings())
    available = [n for n in AUTO_ORDER if has_key(configs[n], secrets)]
    env_values: dict[str, str] = {}
    if provider is not None and provider not in PRESETS:
        err.print(f"[red]Unknown provider {provider!r}[/] (known: {', '.join(PRESETS)})")
        return 1
    if api_key:
        target = provider or "anthropic"
        key_env = PRESETS[target].api_key_env
        if key_env is None:
            err.print(f"[red]{target} does not use an API key[/]")
            return 1
        env_values[key_env] = api_key
        provider = target
    elif provider is None and not available and interactive:
        console.print("\n[bold]Model provider[/]: no API keys found in your environment.")
        names = [n for n in PRESETS]
        for n in names:
            console.print(f"  [cyan]{n:<11}[/] {PRESETS[n].label}")
        provider = Prompt.ask("Provider", choices=names, default="anthropic")
        key_env = PRESETS[provider].api_key_env
        if key_env:
            key = Prompt.ask(f"{key_env}", password=True)
            if key.strip():
                env_values[key_env] = key.strip()
    elif available and interactive and provider is None:
        console.print(
            f"\nFound API keys for: [green]{', '.join(available)}[/]. "
            f"Decapod will use [bold]{available[0]}[/] by default."
        )
    preset = PRESETS.get(provider or (available[0] if available else ""))
    if preset is not None and interactive:
        for tier in Tier:
            if tier not in models and tier not in preset.models:
                models[tier] = Prompt.ask(f"Model for {tier.value} on {preset.name}")

    # ------------------------------------------------------------- write files
    instance_name = name or paths.root.name
    (paths.root / "decapod.toml").write_text(
        render_config(instance_name, provider, models), encoding="utf-8"
    )
    (paths.root / "charter.toml").write_text(charter_text, encoding="utf-8")
    if env_values:
        env_path = user_env_file() if user_wide_key else paths.env
        if (
            interactive
            and not user_wide_key
            and Confirm.ask(f"Save the key for all instances ({user_env_file()})?", default=False)
        ):
            env_path = user_env_file()
        write_env(env_path, env_values)
        console.print(f"Saved API key to {env_path}")
    console.print(f"\n[green]✓[/] wrote {paths.root / 'decapod.toml'}")
    console.print(f"[green]✓[/] wrote {paths.root / 'charter.toml'}  [dim](edit it!)[/]")
    console.print("\nNext steps:")
    console.print("  [bold]decapod doctor[/]       check models, keys and tools")
    console.print("  [bold]decapod start[/]        start the agent in the background")
    console.print("  [bold]decapod tui[/]          watch it think; pause or kill it")
    return 0


def _set_name(charter_text: str, name: str) -> str:
    lines = charter_text.splitlines()
    in_identity = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_identity = stripped == "[identity]"
        elif in_identity and stripped.startswith("name"):
            lines[i] = f'name = "{name}"'
            return "\n".join(lines) + "\n"
    for i, line in enumerate(lines):
        if line.strip() == "[identity]":
            lines.insert(i + 1, f'name = "{name}"')
            break
    return "\n".join(lines) + "\n"
