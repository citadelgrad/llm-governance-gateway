import re
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class MockScenario:
    name: str
    trigger: Callable[[list[dict]], bool]
    decision: str
    expected_status: int
    response_text: str
    violations: list[str] = field(default_factory=list)
    pii_types: list[str] = field(default_factory=list)


def _content(messages: list[dict]) -> str:
    return " ".join(m.get("content", "") for m in messages if isinstance(m.get("content"), str))


clean_request = MockScenario(
    name="clean_request",
    trigger=lambda messages: not re.search(
        r"\d{3}-\d{2}-\d{4}|diagnosis|patient record|ignore previous instructions"
        r"|disregard system|gpt-4o|__rate_limit_test__",
        _content(messages),
        re.IGNORECASE,
    ),
    decision="allow",
    expected_status=200,
    response_text="Here is your answer.",
)

pii_redact = MockScenario(
    name="pii_redact",
    trigger=lambda messages: bool(re.search(r"\d{3}-\d{2}-\d{4}", _content(messages))),
    decision="allow",
    expected_status=200,
    response_text="I can help with that.",
    pii_types=["SSN"],
)

phi_deny = MockScenario(
    name="phi_deny",
    trigger=lambda messages: bool(
        re.search(r"diagnosis|patient record", _content(messages), re.IGNORECASE)
    ),
    decision="block",
    expected_status=403,
    response_text="",
    violations=["policy:data_classification_mismatch"],
)

prompt_injection = MockScenario(
    name="prompt_injection",
    trigger=lambda messages: bool(
        re.search(
            r"ignore previous instructions|disregard system",
            _content(messages),
            re.IGNORECASE,
        )
    ),
    decision="block",
    expected_status=400,
    response_text="",
    violations=["harm:prompt_injection"],
)

model_tier_deny = MockScenario(
    name="model_tier_deny",
    trigger=lambda messages: bool(re.search(r"gpt-4o", _content(messages), re.IGNORECASE)),
    decision="block",
    expected_status=403,
    response_text="",
    violations=["policy:model_tier_denied"],
)

rate_limit_exceed = MockScenario(
    name="rate_limit_exceed",
    trigger=lambda messages: "__rate_limit_test__" in _content(messages),
    decision="allow",
    expected_status=200,
    response_text="",
)

ALL_SCENARIOS: list[MockScenario] = [
    clean_request,
    pii_redact,
    phi_deny,
    prompt_injection,
    model_tier_deny,
    rate_limit_exceed,
]
