from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Any

from transformers import pipeline as hf_pipeline


@dataclass
class HarmResult:
    score: float          # 0.0 = safe, 1.0 = definitely harmful
    blocked: bool         # True if any scanner says is_valid=False
    reason: str           # "prompt_injection" | "banned_topic" | ""


_INJECTION_MODEL = "laiyer/deberta-v3-base-prompt-injection"
_ZERO_SHOT_MODEL = "cross-encoder/nli-deberta-v3-small"
_BANNED_TOPICS = ["violence", "hate", "illegal", "jailbreak"]
_INJECTION_THRESHOLD = 0.5
_BANNED_TOPIC_THRESHOLD = 0.8

_lock = threading.Lock()
_injection_pipe: Any | None = None
_topics_pipe: Any | None = None


def _pipelines() -> tuple[Any, Any]:
    global _injection_pipe, _topics_pipe
    if _injection_pipe is None:
        with _lock:
            if _injection_pipe is None:  # double-checked locking
                _injection_pipe = hf_pipeline("text-classification", model=_INJECTION_MODEL, device="cpu")
                _topics_pipe = hf_pipeline("zero-shot-classification", model=_ZERO_SHOT_MODEL, device="cpu")
    return _injection_pipe, _topics_pipe


def warm_up_scanners() -> None:
    try:
        _pipelines()
    except Exception as exc:
        print(f"[harm] scanner warm-up skipped: {exc}", file=sys.stderr)


def harm_scan(text: str) -> HarmResult:
    injection_pipe, topics_pipe = _pipelines()

    inj_out = injection_pipe(text)[0]
    inj_score = inj_out["score"] if inj_out["label"].upper() == "INJECTION" else 1.0 - inj_out["score"]
    inj_blocked = inj_score >= _INJECTION_THRESHOLD

    topics_out = topics_pipe(text, candidate_labels=_BANNED_TOPICS, multi_label=True)
    top_score = max(topics_out["scores"])
    top_blocked = top_score >= _BANNED_TOPIC_THRESHOLD

    score = max(inj_score, top_score)
    blocked = inj_blocked or top_blocked
    reason = ""
    if inj_blocked:
        reason = "prompt_injection"
    elif top_blocked:
        reason = "banned_topic"

    return HarmResult(score=score, blocked=blocked, reason=reason)
