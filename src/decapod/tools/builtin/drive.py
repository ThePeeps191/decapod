"""The drive: Decapod's sandboxed workspace, plus local Markdown publishing."""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from decapod.domain import RiskTier, SideEffect, ToolResult
from decapod.tools.base import Tool, ToolArgs, ToolContext
from decapod.util.text import truncate

#: Folders the drive tools may not touch. site/ is generated output: only the site builder
#: writes it, so nothing hand-written can be deployed as part of the website.
RESERVED = {".trash", ".sandbox", ".site-build", ".site-old", "site"}


class DriveError(ValueError):
    pass


def resolve_in_drive(drive: Path, relative: str, *, allow_root: bool = False) -> Path:
    """Resolve ``relative`` inside ``drive``; refuse anything that escapes it."""
    text = (relative or ".").strip().replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise DriveError("paths must be relative to the drive")
    root = drive.resolve()
    target = (root / text).resolve()
    if target != root and root not in target.parents:
        raise DriveError("path escapes the drive")
    if target == root and not allow_root:
        raise DriveError("path must name a file inside the drive")
    rel_parts = target.relative_to(root).parts
    if rel_parts and rel_parts[0] in RESERVED and not allow_root:
        raise DriveError(f"'{rel_parts[0]}' is reserved")
    return target


class ListArgs(ToolArgs):
    path: str = "."
    recursive: bool = False


class DriveList(Tool):
    name = "drive.list"
    description = "List files in your drive (your private workspace)."
    Args = ListArgs
    required_scopes = ("drive.read",)

    async def run(self, args: ListArgs, ctx: ToolContext) -> ToolResult:
        try:
            base = resolve_in_drive(ctx.paths.drive, args.path, allow_root=True)
        except DriveError as exc:
            return ToolResult.failure(str(exc))
        if not base.exists():
            return ToolResult.failure(f"no such directory: {args.path}")
        if base.is_file():
            return ToolResult.success(f"{args.path} ({base.stat().st_size} bytes)")
        root = ctx.paths.drive.resolve()
        entries = sorted(base.rglob("*") if args.recursive else base.iterdir())
        lines = []
        for entry in entries:
            rel = entry.relative_to(root)
            if rel.parts and rel.parts[0] in RESERVED:
                continue
            if entry.is_dir():
                lines.append(f"{rel.as_posix()}/")
            else:
                lines.append(f"{rel.as_posix()} ({entry.stat().st_size} bytes)")
            if len(lines) >= 500:
                lines.append("… (truncated)")
                break
        return ToolResult.success("\n".join(lines) or "(empty)")


class ReadArgs(ToolArgs):
    path: str
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=8000, ge=100, le=50_000)


class DriveRead(Tool):
    name = "drive.read"
    description = "Read a text file from your drive."
    Args = ReadArgs
    required_scopes = ("drive.read",)

    async def run(self, args: ReadArgs, ctx: ToolContext) -> ToolResult:
        try:
            path = resolve_in_drive(ctx.paths.drive, args.path)
        except DriveError as exc:
            return ToolResult.failure(str(exc))
        if not path.is_file():
            return ToolResult.failure(f"no such file: {args.path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        chunk = text[args.offset : args.offset + args.max_chars]
        more = len(text) - (args.offset + len(chunk))
        suffix = (
            f"\n…[{more} more chars; continue with offset={args.offset + len(chunk)}]"
            if more > 0
            else ""
        )
        return ToolResult.success(chunk + suffix, {"length": len(text)})


class WriteArgs(ToolArgs):
    path: str
    content: str
    mode: Literal["overwrite", "append"] = "overwrite"


class DriveWrite(Tool):
    name = "drive.write"
    description = "Create, overwrite or append to a text file in your drive."
    Args = WriteArgs
    tier = RiskTier.LOCAL_WRITE
    side_effect = SideEffect.LOCAL
    required_scopes = ("drive.write",)

    def precheck(self, args: WriteArgs, ctx: ToolContext) -> str | None:
        try:
            resolve_in_drive(ctx.paths.drive, args.path)
        except DriveError as exc:
            return str(exc)
        if len(args.content.encode("utf-8")) > ctx.settings.tools.drive_max_file_bytes:
            return "content exceeds the drive's per-file size limit"
        return None

    async def run(self, args: WriteArgs, ctx: ToolContext) -> ToolResult:
        path = resolve_in_drive(ctx.paths.drive, args.path)
        if path.exists() and path.is_dir():
            return ToolResult.failure(f"{args.path} is a directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        if args.mode == "append" and path.exists():
            existing = path.stat().st_size
            if existing + len(args.content.encode()) > ctx.settings.tools.drive_max_file_bytes:
                return ToolResult.failure("append would exceed the per-file size limit")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(args.content)
        else:
            path.write_text(args.content, encoding="utf-8")
        return ToolResult.success(f"wrote {args.path} ({path.stat().st_size} bytes)")


class DeleteArgs(ToolArgs):
    path: str


class DriveDelete(Tool):
    name = "drive.delete"
    description = "Move a file or folder in your drive to the drive's trash (recoverable)."
    Args = DeleteArgs
    tier = RiskTier.LOCAL_WRITE
    side_effect = SideEffect.LOCAL
    required_scopes = ("drive.write",)

    async def run(self, args: DeleteArgs, ctx: ToolContext) -> ToolResult:
        try:
            path = resolve_in_drive(ctx.paths.drive, args.path)
        except DriveError as exc:
            return ToolResult.failure(str(exc))
        if not path.exists():
            return ToolResult.failure(f"no such path: {args.path}")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        trash = ctx.paths.drive / ".trash" / stamp
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(trash / path.name))
        return ToolResult.success(f"moved {args.path} to trash")


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:max_len].rstrip("-")) or "note"


class PublishArgs(ToolArgs):
    title: str = Field(min_length=3, max_length=200)
    content: str = Field(min_length=10, description="Markdown body")
    tags: list[str] = Field(default_factory=list)


class NotesPublish(Tool):
    name = "notes.publish"
    description = (
        "Publish a finished Markdown note to drive/published/ and update its index. "
        "This is your safe, local way to produce public-ready work."
    )
    Args = PublishArgs
    tier = RiskTier.LOCAL_WRITE
    side_effect = SideEffect.LOCAL
    required_scopes = ("notes.publish",)

    async def run(self, args: PublishArgs, ctx: ToolContext) -> ToolResult:
        folder = ctx.paths.drive / "published"
        folder.mkdir(parents=True, exist_ok=True)
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        base = f"{date}-{slugify(args.title)}"
        path = folder / f"{base}.md"
        n = 2
        while path.exists():
            path = folder / f"{base}-{n}.md"
            n += 1
        tags = ", ".join(args.tags)
        front = (
            f"---\ntitle: {args.title!r}\ndate: {date}\nauthor: {ctx.charter.name} "
            f"(autonomous AI agent)\ntags: [{tags}]\n---\n\n"
        )
        path.write_text(front + args.content.strip() + "\n", encoding="utf-8")
        index = folder / "index.md"
        header = f"# {ctx.charter.name}: published notes\n\n"
        existing = index.read_text(encoding="utf-8") if index.exists() else header
        if not existing.startswith("#"):
            existing = header + existing
        lines = existing.rstrip("\n").split("\n")
        entry = f"- {date}: [{args.title}]({path.name})"
        lines.insert(2 if len(lines) >= 2 else len(lines), entry)
        index.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rel = path.relative_to(ctx.paths.drive).as_posix()
        site_note = ""
        if ctx.settings.tools.site.build:
            from decapod.tools.builtin.site import rebuild

            try:
                result = rebuild(ctx)
                site_note = f"; website rebuilt ({len(result.notes)} notes in drive/site/)"
            except Exception as exc:  # the note is published even if the site build fails
                site_note = f"; website rebuild failed: {exc}"
        return ToolResult.success(f"published {rel}{site_note}", {"path": rel})


class ReadPublishedArgs(ToolArgs):
    max_chars: int = Field(default=4000, ge=200, le=20_000)


class NotesIndex(Tool):
    name = "notes.index"
    description = "Show the index of notes you have published."
    Args = ReadPublishedArgs
    required_scopes = ("drive.read",)

    async def run(self, args: ReadPublishedArgs, ctx: ToolContext) -> ToolResult:
        index = ctx.paths.drive / "published" / "index.md"
        if not index.exists():
            return ToolResult.success("nothing published yet")
        return ToolResult.success(truncate(index.read_text(encoding="utf-8"), args.max_chars))


DRIVE_TOOLS: list[type[Tool]] = [
    DriveList,
    DriveRead,
    DriveWrite,
    DriveDelete,
    NotesPublish,
    NotesIndex,
]
