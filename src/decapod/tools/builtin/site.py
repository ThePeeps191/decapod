"""Build the website from published notes, and deploy it to GitHub Pages."""

from __future__ import annotations

import base64
import html
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from pydantic import Field

from decapod.config import Secrets, Settings
from decapod.domain import RiskTier, SideEffect, ToolResult
from decapod.publishing import SiteResult, build_site
from decapod.tools.base import NoArgs, Tool, ToolArgs, ToolContext
from decapod.tools.builtin.github import _GitHubTool, safe_path
from decapod.util.text import one_line


def rebuild(ctx: ToolContext) -> SiteResult:
    cfg = ctx.settings.tools.site
    return build_site(
        ctx.paths.drive,
        title=cfg.title or ctx.charter.name,
        description=cfg.description or ctx.charter.identity.mission,
        author=ctx.charter.name,
        base_url=cfg.base_url,
    )


class SiteBuild(Tool):
    name = "site.build"
    description = "Rebuild your website (drive/site/: HTML pages and an RSS feed) from your notes."
    Args = NoArgs
    tier = RiskTier.LOCAL_WRITE
    side_effect = SideEffect.LOCAL
    required_scopes = ("notes.publish",)

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        result = rebuild(ctx)
        return ToolResult.success(
            f"built drive/site/ with {len(result.notes)} notes ({len(result.files)} files)",
            {"files": result.files},
        )


class DeployArgs(ToolArgs):
    message: str = Field(default="Update site", min_length=3, max_length=200)


class SiteDeploy(_GitHubTool):
    name = "site.deploy"
    description = (
        "Publish your website (drive/site/) to its GitHub Pages repository in a "
        "single commit. This makes your notes public."
    )
    Args = DeployArgs
    tier = RiskTier.EXTERNAL_WRITE
    side_effect = SideEffect.EXTERNAL_WRITE
    access = "write"
    outbound = True

    def scopes(self, args: Any) -> list[str]:
        return [f"github:{self._repo}:write"]

    _repo = ""

    def unavailable_reason(self, settings: Settings, secrets: Secrets) -> str | None:
        repo = settings.tools.site.repo
        if not repo:
            return "set [tools.site] repo to the GitHub Pages repository (e.g. me/me.github.io)"
        self._repo = repo
        return super().unavailable_reason(settings, secrets)

    def precheck(self, args: DeployArgs, ctx: ToolContext) -> str | None:
        published = ctx.paths.drive / "published"
        if not published.exists() or not any(p.name != "index.md" for p in published.glob("*.md")):
            return "nothing to deploy: publish a note first"
        return None

    async def run(self, args: DeployArgs, ctx: ToolContext) -> ToolResult:
        cfg = ctx.settings.tools.site
        repo = cfg.repo or ""
        # Always deploy a fresh build, and only the files the builder produced.
        result = rebuild(ctx)
        site = result.path
        prefix = safe_path(cfg.path) + "/" if cfg.path.strip("/") else ""
        files = [site / rel for rel in result.files]
        leak = find_secret(ctx, [*sorted((ctx.paths.drive / "published").glob("*.md")), *files])
        if leak:
            return ToolResult.failure(f"refused: {leak} contains a configured secret")

        branch = cfg.branch
        ref = await self.request(ctx, "GET", f"/repos/{repo}/git/ref/heads/{branch}")
        parent: str | None = None
        base_tree: str | None = None
        if ref.status_code == 200:
            parent = ref.json()["object"]["sha"]
            commit = await self.request(ctx, "GET", f"/repos/{repo}/git/commits/{parent}")
            if commit.status_code >= 400:
                return _fail(commit)
            base_tree = commit.json()["tree"]["sha"]
        elif ref.status_code != 404:
            return _fail(ref)

        entries: list[dict[str, Any]] = []
        for file in files:
            blob = await self.request(
                ctx,
                "POST",
                f"/repos/{repo}/git/blobs",
                json={
                    "content": base64.b64encode(file.read_bytes()).decode("ascii"),
                    "encoding": "base64",
                },
            )
            if blob.status_code >= 400:
                return _fail(blob)
            rel = file.relative_to(site).as_posix()
            entries.append(
                {"path": prefix + rel, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]}
            )
        if base_tree:
            # Remove pages of notes that no longer exist (other files, e.g. CNAME, stay).
            current = {e["path"] for e in entries}
            old = await self.request(
                ctx, "GET", f"/repos/{repo}/git/trees/{base_tree}", params={"recursive": "1"}
            )
            if old.status_code >= 400:
                return _fail(old)
            for item in old.json().get("tree", []):
                path = item.get("path", "")
                if (
                    item.get("type") == "blob"
                    and path.startswith(prefix + "posts/")
                    and path.endswith(".html")
                    and path not in current
                ):
                    entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
        tree_body: dict[str, Any] = {"tree": entries}
        if base_tree:
            tree_body["base_tree"] = base_tree
        tree = await self.request(ctx, "POST", f"/repos/{repo}/git/trees", json=tree_body)
        if tree.status_code >= 400:
            return _fail(tree)
        message = (
            f"{args.message}\n\nPublished by {ctx.charter.name}, an autonomous AI agent (Decapod)."
        )
        commit = await self.request(
            ctx,
            "POST",
            f"/repos/{repo}/git/commits",
            json={
                "message": message,
                "tree": tree.json()["sha"],
                "parents": [parent] if parent else [],
            },
        )
        if commit.status_code >= 400:
            return _fail(commit)
        sha = commit.json()["sha"]
        if parent:
            update = await self.request(
                ctx, "PATCH", f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": sha}
            )
        else:
            update = await self.request(
                ctx,
                "POST",
                f"/repos/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": sha},
            )
        if update.status_code >= 400:
            return _fail(update)
        url = cfg.base_url or f"https://github.com/{repo}/tree/{branch}"
        removed = sum(1 for e in entries if e["sha"] is None)
        return ToolResult.success(
            f"deployed {len(files)} files to {repo}@{branch}"
            + (f" (removed {removed} stale pages)" if removed else "")
            + f": {url}",
            {"commit": sha, "files": len(files), "removed": removed},
        )


def secret_variants(secret: str) -> set[str]:
    """How a secret can appear in generated HTML/XML (escaped once or twice)."""
    once = html.escape(secret, quote=True)
    return {
        secret,
        once,
        html.escape(once, quote=True),
        html.escape(secret, quote=False),
        xml_escape(secret),
        xml_escape(once),
    }


def find_secret(ctx: ToolContext, files: list[Any]) -> str | None:
    variants = {v for s in ctx.secrets.values_for_scrubbing() for v in secret_variants(s)}
    if not variants:
        return None
    for file in files:
        text = file.read_text(encoding="utf-8", errors="ignore")
        if any(v in text for v in variants):
            return str(file.name)
    return None


def _fail(response: Any) -> ToolResult:
    try:
        message = response.json().get("message", response.text)
    except ValueError:
        message = response.text
    return ToolResult.failure(f"GitHub API {response.status_code}: {one_line(message, 300)}")


SITE_TOOLS: list[type[Tool]] = [SiteBuild, SiteDeploy]
