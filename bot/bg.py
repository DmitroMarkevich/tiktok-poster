"""Fire-and-forget background task helper.

`asyncio.create_task` / `ensure_future` return a task the event loop holds only a WEAK
reference to — if the caller doesn't keep a strong reference, the GC can collect the task
mid-flight and the background job silently dies (documented CPython footgun). Every
fire-and-forget spawn in the bot (session checks, uploads, comment runs, the autopilot
loop) must go through `spawn()` so a strong reference lives until the task finishes.
"""
import asyncio

_TASKS: set = set()


def spawn(coro):
    """Schedule `coro` as a tracked background task and return it. The task is held in a
    module-level set (strong ref) and auto-discarded on completion, so it can't be GC'd
    while running."""
    t = asyncio.ensure_future(coro)
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
    return t
