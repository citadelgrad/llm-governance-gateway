import pytest

from app import pii


@pytest.mark.asyncio
async def test_ssn_shaped_test_value_is_detected_and_redacted():
    await pii.initialize("en_core_web_sm")

    result = await pii.run("My SSN is 123-45-6789, can you help?")

    assert result.findings
    assert {finding["type"] for finding in result.findings} == {"US_SSN"}
    assert "123-45-6789" not in result.redacted_text
    assert result.data_classification == "pii"
