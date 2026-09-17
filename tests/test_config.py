import pytest

from vnr.config import AsrConfig, NebiusConfig, ResearchBudget, ServiceConfig, Settings
from vnr.errors import ConfigError


def test_defaults_when_env_is_empty():
    settings = Settings.load(env={})
    assert settings.nebius.model == "nvidia/Nemotron-3_5-Lightning"
    assert settings.nebius.base_url == "https://api.studio.nebius.com/v1"
    assert settings.budget.max_turns == 6
    assert settings.budget.max_searches == 4
    assert settings.budget.max_results_per_search == 5
    assert settings.service.host == "127.0.0.1"


def test_env_overrides_are_typed():
    settings = Settings.load(
        env={
            "RESEARCH_MAX_TURNS": "3",
            "RESEARCH_MAX_SEARCHES": "2",
            "RESEARCH_ALLOW_ADVANCED": "false",
            "VNR_ASR_FRAME_MS": "40",
        }
    )
    assert settings.budget.max_turns == 3
    assert settings.budget.max_searches == 2
    assert settings.budget.allow_advanced is False
    assert settings.asr.frame_ms == 40


def test_fast_depth_is_an_alias_for_basic():
    assert ResearchBudget.from_env({"RESEARCH_DEFAULT_DEPTH": "fast"}).default_depth == "basic"


@pytest.mark.parametrize(
    "env",
    [
        {"RESEARCH_MAX_TURNS": "many"},
        {"RESEARCH_ALLOW_ADVANCED": "maybe"},
        {"RESEARCH_DEFAULT_DEPTH": "turbo"},
        {"RESEARCH_MAX_SEARCHES": "0"},
    ],
)
def test_invalid_values_raise_config_error(env):
    with pytest.raises(ConfigError):
        ResearchBudget.from_env(env)


def test_service_refuses_to_leave_loopback():
    with pytest.raises(ConfigError):
        ServiceConfig.from_env({"VNR_SERVICE_HOST": "0.0.0.0"})
    assert ServiceConfig.from_env({"VNR_SERVICE_PORT": "9000"}).port == 9000


def test_missing_credentials_only_fail_at_point_of_use():
    settings = Settings.load(env={})  # loading must work on a machine with no keys
    with pytest.raises(ConfigError):
        settings.nebius.require_key()
    with pytest.raises(ConfigError):
        settings.tavily.require_key()
    assert NebiusConfig(api_key="k").require_key() == "k"


def test_extra_body_must_be_a_json_object():
    config = NebiusConfig.from_env(
        {"NEBIUS_EXTRA_BODY": '{"chat_template_kwargs":{"thinking":false}}'}
    )
    assert config.extra_body == {"chat_template_kwargs": {"thinking": False}}
    with pytest.raises(ConfigError):
        NebiusConfig.from_env({"NEBIUS_EXTRA_BODY": "[1,2]"})
    with pytest.raises(ConfigError):
        NebiusConfig.from_env({"NEBIUS_EXTRA_BODY": "not json"})


def test_asr_frame_samples():
    assert AsrConfig(sample_rate=24_000, frame_ms=80).frame_samples == 1920
