from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "live_smoke.py"
SPEC = importlib.util.spec_from_file_location("live_smoke", SCRIPT)
assert SPEC and SPEC.loader
live_smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = live_smoke
SPEC.loader.exec_module(live_smoke)


def test_responses_output_text_accepts_native_responses_wire_shape():
    body = {
        "object": "response",
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "Salem", "annotations": []}
                ],
            }
        ],
    }

    assert live_smoke._responses_output_text(body) == "Salem"


def test_responses_output_text_accepts_translated_gateway_convenience_field():
    assert live_smoke._responses_output_text({"output_text": "Salem"}) == "Salem"
