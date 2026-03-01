"""Entry point: uv run python -m matrix_agent"""

import asyncio
import logging

from .config import Settings
from .sandbox import SandboxManager
from .decider import Decider
from .core import TaskRunner
from .bot import Bot
from .channels import GitHubChannel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


async def main():
    settings = Settings()
    sandbox = SandboxManager(settings)
    decider = Decider(settings, sandbox)
    task_runner = TaskRunner(decider, sandbox)

    # Load persisted state, restore histories, destroy orphan containers
    histories = await sandbox.load_state()
    decider.load_histories(histories)
    await task_runner.destroy_orphans()

    bot = Bot(settings, sandbox, decider, task_runner)

    # Only start GitHub channel if a token is configured
    github_channel = None
    if settings.github_token:
        github_channel = GitHubChannel(task_runner=task_runner, settings=settings)
        await github_channel.start()

    try:
        await bot.run()
    finally:
        if github_channel:
            await github_channel.stop()


if __name__ == "__main__":
    asyncio.run(main())
