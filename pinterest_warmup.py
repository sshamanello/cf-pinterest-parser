"""
Pinterest account warmup via DroidRun + local Ollama LLM.
Simulates natural user behaviour: scroll feed, save pins, browse boards.
No vision model needed — uses accessibility tree (text only).
"""
import asyncio
import logging
import os
import random

logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

_WARMUP_TASKS = [
    (
        "Open the Pinterest app. Scroll through the home feed slowly, as a real person would. "
        "Find 2 pins about fonts or typography that look interesting and save them to any board. "
        "Then scroll a bit more and close the app. Take your time, don't rush."
    ),
    (
        "Open Pinterest. Browse the home feed for a few minutes. "
        "Tap on one pin to view it in detail, then go back. "
        "Save 1-2 pins that look visually appealing. Close the app when done."
    ),
    (
        "Open Pinterest and go to the Search tab. "
        "Search for 'modern fonts' or 'typography design'. "
        "Scroll through results, save 2 pins you find interesting. "
        "Go back to home feed, scroll briefly, then close the app."
    ),
    (
        "Open Pinterest. Scroll the home feed slowly. "
        "Save 1 pin to a board. Scroll a bit more, then close the app naturally."
    ),
]


async def warmup_device(serial: str) -> bool:
    """
    Run a warmup session on the given device using DroidRun + Ollama.
    Returns True on success.
    """
    try:
        import droidrun
    except ImportError:
        logger.error("droidrun not installed. Run: pip install droidrun==0.4.26")
        return False

    goal = random.choice(_WARMUP_TASKS)
    logger.info("[%s] Starting warmup | goal: %s", serial, goal[:80] + "...")

    try:
        llm = droidrun.load_llm(
            "ollama",
            model=OLLAMA_MODEL,
            base_url=OLLAMA_URL,
        )

        tools = droidrun.AdbTools(
            serial=serial,
            vision_enabled=False,  # accessibility tree only — no vision model needed
        )

        agent = droidrun.DroidAgent(
            goal=goal,
            llms=[llm],
            tools=tools,
        )

        await agent.run()
        logger.info("[%s] ✓ Warmup complete", serial)
        return True
    except Exception as e:
        logger.error("[%s] Warmup failed: %s", serial, e)
        return False


def warmup_device_sync(serial: str) -> bool:
    return asyncio.run(warmup_device(serial))
