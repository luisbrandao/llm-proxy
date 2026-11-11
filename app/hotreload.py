"""Bringing the running process in line with a changed config file.

`config.reload_if_changed()` swaps the config itself. Two things in the process
were derived from the old one and have to follow it: the discovery cache (a
changed `base_url` or allow-list makes a cached catalog wrong) and the slot
queue (a changed `slots` may have created capacity a request is waiting for).

Both ways a config changes go through `apply`: the watcher, on a timer, for an
edit made on the host, and the admin API, right after a console edit.
"""
import asyncio
import logging

from app import config as conf
from app import registry, slots

logger = logging.getLogger("llm-proxy")


async def apply(how: str) -> bool:
    """Reload the config if the file changed. True when a new one is now live.

    A file that does not parse raises, with the running config left in place.
    """
    if not conf.reload_if_changed():
        return False
    registry.clear_cache()
    await slots.poke()
    logger.info(
        "Config reloaded %s: %d providers, %d logical models, %d aliases",
        how, len(conf.PROVIDERS), len(conf.LOGICAL_MODELS), len(conf.ALIASES),
    )
    return True


async def after_console_edit() -> bool:
    """Pull a just-written config edit into the live process, now.

    The watcher would find it within CONFIG_RELOAD_INTERVAL, but a console edit
    has to be in effect before the response lands — otherwise the UI re-reads and
    renders the pre-edit state, which reads as "my change didn't save".
    """
    try:
        return await apply("after a console edit")
    except Exception as e:  # noqa: BLE001 - a write we validated should parse, but
        logger.warning("Config written but reload failed: %s: %s", type(e).__name__, e)
        return False


async def watch() -> None:
    """Hot-apply edits to the config file, no restart needed.

    Polls every CONFIG_RELOAD_INTERVAL seconds. On a change the module globals
    are swapped (in-flight requests finish under the old Provider objects). A
    bad edit (YAML typo, torn read mid-write by an external editor) is logged
    and skipped — the running config stays until the file parses again.

    The console's own edits (configwrite) also land here one tick later; that
    reload is a no-op, since `after_console_edit` already applied them.
    """
    while True:
        await asyncio.sleep(conf.CONFIG_RELOAD_INTERVAL)
        try:
            await apply(f"from {conf.CONFIG_PATH}")
        except FileNotFoundError:
            # Transient hole while an editor/deploy replaces the file; the
            # next tick sees the settled result. Keep the running config.
            pass
        except Exception as e:  # noqa: BLE001 - a bad edit must never kill the app
            logger.warning(
                "Config reload failed — keeping the running config: %s: %s",
                type(e).__name__, e,
            )
