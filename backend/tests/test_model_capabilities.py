import pytest

from forge.model_capabilities import output_limit
from forge.providers import parameter_defaults, validate_output_limit


def test_known_and_unknown_output_limits_are_explicit():
    assert output_limit('gpt-5.6-sol') == 128000
    assert output_limit('gpt-5.6-sol', 64000) == 64000
    assert output_limit('private-model') == 4096
    assert parameter_defaults('openai_responses', {}, 'gpt-5.6-sol')[1] == {'max_output_tokens': 128000}


def test_output_limit_rejects_values_above_known_cap():
    with pytest.raises(ValueError, match='已知上限'):
        validate_output_limit('openai_responses', 'gpt-5.6-sol', None, {'max_output_tokens': 128001})
