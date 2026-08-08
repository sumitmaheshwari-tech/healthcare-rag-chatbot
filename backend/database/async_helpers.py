import asyncio
from functools import partial

async def run_db(func, *args, **kwargs):
    """Run a synchronous DB function in a thread pool to avoid blocking the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))
