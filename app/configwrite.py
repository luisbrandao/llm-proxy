"""Persist runtime routing edits back into the config file, surgically.

`POST /admin/routing/{model}` changes target priorities in memory; this module
makes that change durable by rewriting **only the `priority: N` digits** on the
matching target lines of `CONFIG_PATH`. Everything else — comments, alignment,
key order, quoting — is preserved byte-for-byte, so the file stays pleasant to
read and a GitOps working tree shows a minimal, reviewable diff.

Why not a YAML round-trip? PyYAML drops every comment; ruamel.yaml (a new
dependency) re-emits the whole file and normalizes whitespace file-wide, which
turns the first runtime edit into a noisy diff. Targeted text surgery has
neither problem, at the cost of only supporting the flow style this project
already uses everywhere: `- {provider: X, model: "Y", priority: N}`.

Safety model: abort-don't-corrupt. Any surprise — a target line the regex can't
read, a target present in the file but not in the request (or vice versa), a
failed parse-back self-check — aborts the persist with a reason, leaving the
file untouched. The caller keeps the live in-memory change and reports the
failure; it never half-writes.

The write is IN-PLACE (`r+` + truncate), never write-temp-then-rename:
`/app/config.yaml` is a single-file bind mount, so the mount is pinned to the
host file's inode — a rename would swap the directory entry while the container
keeps the old inode (and renaming over a mount point fails outright). The
content is fully validated in memory before the file is opened for writing.
"""
import asyncio
import logging
import os
import re

import yaml

from app import config as conf

logger = logging.getLogger("llm-proxy")

# Serializes the read-modify-write cycle. Created lazily so it binds to the
# running loop, matching the convention in slots/registry.
_lock = None


def _lock_for() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


_MODELS_RE = re.compile(r"^models:\s*(#.*)?$")
_TARGET_RE = re.compile(r"-\s*\{(.*)\}")
_PROVIDER_RE = re.compile(r"provider:\s*([^,}\s]+)")
_MODEL_RE = re.compile(r'model:\s*("(?:[^"]*)"|[^,}]+)')
_PRIORITY_RE = re.compile(r"(priority:\s*)(\d+)")


def config_writable() -> bool:
    """Whether the config file accepts writes (False on a `:ro` bind mount)."""
    return os.access(conf.CONFIG_PATH, os.W_OK)


def _rewrite(text: str, model_name: str, wanted: dict):
    """Return (new_text, None) with priorities rewritten, or (None, reason).

    `wanted` maps (provider, model-or-None) -> new priority, where `model` is the
    target's as-written value (None when inherited via model_map) — the same
    identity the config loader produces, so it matches the YAML line exactly.
    Matching is by key, never by position: file order and priority order already
    diverge in real configs.
    """
    lines = text.split("\n")

    # Bound the top-level `models:` section (ends at the next column-0 key).
    start = next((i for i, l in enumerate(lines) if _MODELS_RE.match(l)), None)
    if start is None:
        return None, "no top-level 'models:' section in config"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i] and lines[i][0] not in " #"),
        len(lines),
    )

    # Find this model's key line within the section.
    key_re = re.compile(r"^(\s+)" + re.escape(model_name) + r":\s*(#.*)?$")
    key_at = next((i for i in range(start + 1, end) if key_re.match(lines[i])), None)
    if key_at is None:
        return None, f"model '{model_name}' not found under models:"
    indent = len(key_re.match(lines[key_at]).group(1))

    # Walk the model's block (until a line dedents back to key level or less)
    # and rewrite each flow-style target line.
    matched = set()
    for i in range(key_at + 1, end):
        line = lines[i]
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and len(line) - len(line.lstrip()) <= indent:
            break  # next model
        t = _TARGET_RE.search(line)
        if not t:
            continue
        body = t.group(1)
        pm = _PROVIDER_RE.search(body)
        if not pm:
            return None, f"target line without provider: {stripped}"
        mm = _MODEL_RE.search(body)
        key = (pm.group(1), mm.group(1).strip().strip('"') if mm else None)
        if key not in wanted:
            return None, f"config target {key} not in the request (out of sync?)"
        if key in matched:
            return None, f"duplicate target {key} in config"
        if not _PRIORITY_RE.search(line):
            return None, f"target line without a priority field: {stripped}"
        matched.add(key)
        lines[i] = _PRIORITY_RE.sub(lambda m: m.group(1) + str(wanted[key]), line, count=1)

    if matched != set(wanted):
        missing = set(wanted) - matched
        return None, f"targets not found as flow-style lines in config: {sorted(missing)}"
    return "\n".join(lines), None


def _self_check(new_text: str, model_name: str, wanted: dict):
    """Parse the rewritten text and verify it means exactly what was requested."""
    try:
        data = yaml.safe_load(new_text)
        got = {
            (t["provider"], t.get("model")): int(t.get("priority", 100))
            for t in data["models"][model_name]["targets"]
        }
    except Exception as e:  # noqa: BLE001 - any parse trouble means do not write
        return f"rewritten config failed to parse: {type(e).__name__}: {e}"
    if got != wanted:
        return f"self-check mismatch after rewrite: {got} != {wanted}"
    return None


async def persist_model_priorities(model_name: str, targets) -> tuple:
    """Write the given targets' priorities into CONFIG_PATH. -> (ok, reason).

    `targets` are the live, already-updated `LogicalModel.targets`. Failure never
    raises and never partially writes; the caller decides how to surface it.
    """
    wanted = {(t.provider, t.model): t.priority for t in targets}
    if len(wanted) != len(targets):
        return False, "duplicate (provider, model) among targets"

    async with _lock_for():
        try:
            with open(conf.CONFIG_PATH) as f:
                text = f.read()
        except OSError as e:
            return False, f"cannot read config: {e}"

        new_text, err = _rewrite(text, model_name, wanted)
        if err:
            return False, err
        if new_text == text:
            return True, None  # nothing to do (priorities already match)

        err = _self_check(new_text, model_name, wanted)
        if err:
            return False, err

        try:
            # In-place: keep the bind-mounted inode (see module docstring).
            with open(conf.CONFIG_PATH, "r+") as f:
                f.write(new_text)
                f.truncate()
        except OSError as e:
            # Typically EROFS/EACCES on a read-only mount: expected on deploys
            # that haven't switched the mount to rw yet.
            return False, f"config not writable: {e}"

    logger.info("Persisted routing priorities for '%s' to %s", model_name, conf.CONFIG_PATH)
    return True, None
