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

# Warmup task templates — pick one randomly each session
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
        "Like (react to) 2-3 pins. Save 1 pin to a board. "
        "Scroll a bit more, then close the app naturally."
    ),
]


async def warmup_device(serial: str, duration_minutes: int = 5) -> bool:
    """
    Run a warmup session on the given device using DroidRun + Ollama.
    Returns True on success.
    """
    try:
        from droidrun.agent import DroidAgent
        from droidrun.tools import ADBTools
    except ImportError:
        logger.error("droidrun not installed. Run: pip install droidrun")
        return False

    task = random.choice(_WARMUP_TASKS)
    logger.info("[%s] Starting warmup session (~%d min)", serial, duration_minutes)
    logger.info("[%s] Task: %s", serial, task[:80] + "...")

    try:
        from langchain_ollama import ChatOllama
        llm = ChatOllama(
            base_url=OLLAMA_URL,
            model=OLLAMA_MODEL,
            temperature=0.7,
        )
    except ImportError:
        # Fallback: try droidrun's built-in Ollama support
        from droidrun.llm import get_llm
        llm = get_llm(provider="ollama", model=OLLAMA_MODEL, base_url=OLLAMA_URL)

    tools = ADBTools(device_serial=serial)
    agent = DroidAgent(
        task=task,
        adb_tools=tools,
        llm=llm,
        use_screenshot=False,  # accessibility tree only — no vision model needed
        max_steps=30,
    )

    try:
        await agent.run()
        logger.info("[%s] ✓ Warmup session complete", serial)
        return True
    except Exception as e:
        logger.error("[%s] Warmup failed: %s", serial, e)
        return False


def warmup_device_sync(serial: str, duration_minutes: int = 5) -> bool:
    return asyncio.run(warmup_device(serial, duration_minutes))
