"""Entry point: uv run python -m matrix_agent"""

import asyncio
import logging
import os
import signal

from .config import Settings
from .sandbox import SandboxManager
from .decider import Decider
from .core import TaskRunner
from .bot import Bot
from .channels import GitHubChannel

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


async def main():
    settings = Settings()
    sandbox = SandboxManager(settings)
    decider = Decider(settings, sandbox)
    task_runner = TaskRunner(decider, sandbox)

    # Load persisted state and restore histories
    histories = await sandbox.load_state()
    decider.load_histories(histories)

    # GitHub recovery: scan for open issues before starting webhook server
    github_channel = None
    if settings.github_token:
        github_channel = GitHubChannel(task_runner=task_runner, settings=settings)
        recovered = await github_channel.recover_tasks()
        await github_channel.start()
        for task_id, msg in recovered:
            await task_runner.enqueue(task_id, msg, github_channel)

    # Matrix recovery: sync + pre_register surviving rooms
    bot = Bot(settings, sandbox, decider, task_runner)
    await bot.setup()

    # Now _processing contains all recovered tasks — safe to destroy orphans
    await task_runner.destroy_orphans()

    shutdown_event = asyncio.Event()

    def handle_signal():
        logging.info("Received shutdown signal")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    # Run bot until signal
    bot_task = asyncio.create_task(bot.run())
    
    # Wait for shutdown signal
    await shutdown_event.wait()
    
    logging.info("Starting graceful shutdown...")
    
    # Graceful teardown
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass
        
    await task_runner.shutdown()
    sandbox.save_state()
    
    if github_channel:
        await github_channel.stop()
    
    logging.info("Bot exited cleanly")


if __name__ == "__main__":
    asyncio.run(main())
