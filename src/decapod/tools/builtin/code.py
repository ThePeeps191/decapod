"""Sandboxed Python execution inside the drive."""

from __future__ import annotations

from pydantic import Field

from decapod.config import Secrets, Settings
from decapod.domain import RiskTier, SideEffect, ToolResult
from decapod.tools.base import Tool, ToolArgs, ToolContext
from decapod.tools.sandbox import effective_isolation, run_python


class CodeArgs(ToolArgs):
    code: str = Field(min_length=1, max_length=50_000, description="Python 3 source")
    timeout_s: int = Field(default=30, ge=1, le=300)


class CodeRun(Tool):
    name = "code.run"
    description = (
        "Run Python 3 in an isolated sandbox. The working directory is "
        "drive/sandbox/ (files persist there). Standard library only; no network "
        "unless the operator allowed it."
    )
    Args = CodeArgs
    tier = RiskTier.LOCAL_WRITE
    side_effect = SideEffect.LOCAL
    required_scopes = ("code.run",)

    def unavailable_reason(self, settings: Settings, secrets: Secrets) -> str | None:
        if effective_isolation(settings.tools.code.isolation) is None:
            return (
                "bubblewrap (bwrap) is not installed or not usable here; install it or set "
                'tools.code.isolation = "none" to allow unisolated execution'
            )
        return None

    async def run(self, args: CodeArgs, ctx: ToolContext) -> ToolResult:
        cfg = ctx.settings.tools.code
        timeout = min(float(args.timeout_s), cfg.timeout.total_seconds())
        try:
            result = await run_python(
                args.code,
                workdir=ctx.paths.drive / "sandbox",
                instance_root=ctx.paths.root,
                timeout_s=timeout,
                memory_mb=cfg.memory_mb,
                allow_network=cfg.allow_network,
                isolation=cfg.isolation,
                procs=ctx.procs,
            )
        except RuntimeError as exc:
            return ToolResult.failure(str(exc))
        parts = [f"exit code: {result.exit_code}" + (" (timed out)" if result.timed_out else "")]
        if result.stdout:
            parts.append("stdout:\n" + result.stdout)
        if result.stderr:
            parts.append("stderr:\n" + result.stderr)
        output = "\n".join(parts)
        data = {
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "isolation": result.isolation,
        }
        if result.timed_out or result.exit_code != 0:
            return ToolResult(
                ok=False, output=output, error="program failed or timed out", data=data
            )
        return ToolResult.success(output, data)


CODE_TOOLS: list[type[Tool]] = [CodeRun]
