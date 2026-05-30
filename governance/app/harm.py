from __future__ import annotations

import threading
from dataclasses import dataclass

from llm_guard.input_scanners import PromptInjection, BanTopics

@dataclass
class HarmResult:
    score: float          # 0.0 = safe, 1.0 = definitely harmful
    blocked: bool         # True if any scanner says is_valid=False
    reason: str           # "prompt_injection" | "banned_topic" | ""

# Task 7: protect lazy-init against concurrent asyncio.to_thread races
_lock = threading.Lock()
_injection_scanner: PromptInjection | None = None
_topics_scanner: BanTopics | None = None

def _scanners() -> tuple[PromptInjection, BanTopics]:
    global _injection_scanner, _topics_scanner
    if _injection_scanner is None:
        with _lock:
            if _injection_scanner is None:  # double-checked locking
                _injection_scanner = PromptInjection()
                _topics_scanner = BanTopics(topics=["violence", "hate", "illegal", "jailbreak"])
    return _injection_scanner, _topics_scanner

def harm_scan(text: str) -> HarmResult:
    injection, topics = _scanners()

    _, inj_valid, inj_score = injection.scan(prompt=text, output="")
    _, top_valid, top_score = topics.scan(prompt=text, output="")

    score = max(inj_score, top_score)
    blocked = not (inj_valid and top_valid)
    reason = ""
    if not inj_valid:
        reason = "prompt_injection"
    elif not top_valid:
        reason = "banned_topic"

    return HarmResult(score=score, blocked=blocked, reason=reason)
