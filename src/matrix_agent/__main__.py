"""Entry point: uv run python -m matrix_agent"""

import asyncio
import logging

from .config import Settings
from .sandbox import SandboxManager
from .agent import Agent
from .bot import Bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


async def main():
    settings = Settings()
    sandbox = SandboxManager(settings)
    agent = Agent(settings, sandbox)
    bot = Bot(settings, sandbox, agent)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
