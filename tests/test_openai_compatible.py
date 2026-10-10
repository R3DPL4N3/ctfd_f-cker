from __future__ import annotations

from types import SimpleNamespace

import backend.models as models
from backend.config import Settings


def test_settings_reads_openai_compatible_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "evren_llm_test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.internal/v1")

    settings = Settings(_env_file=None)

    assert settings.openai_api_key == "evren_llm_test"
    assert settings.openai_base_url == "https://example.internal/v1"


def test_openai_provider_uses_configured_base_url(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, **kwargs) -> None:
            captured["provider_kwargs"] = kwargs

    class FakeModel:
        def __init__(self, model_id: str, provider) -> None:
            captured["model_id"] = model_id
            captured["provider"] = provider

    monkeypatch.setattr(models, "OpenAIProvider", FakeProvider)
    monkeypatch.setattr(models, "OpenAIModel", FakeModel)

    settings = SimpleNamespace(
        openai_api_key="evren_llm_test",
        openai_base_url="https://example.internal/v1",
    )
    model = models.resolve_model("openai/glm-5.3", settings)

    assert model is captured["provider"] or model is not None
    assert captured["model_id"] == "glm-5.3"
    assert captured["provider_kwargs"] == {
        "api_key": "evren_llm_test",
        "base_url": "https://example.internal/v1",
    }


def test_openai_provider_omits_empty_base_url(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, **kwargs) -> None:
            captured["provider_kwargs"] = kwargs

    class FakeModel:
        def __init__(self, model_id: str, provider) -> None:
            captured["model_id"] = model_id
            captured["provider"] = provider

    monkeypatch.setattr(models, "OpenAIProvider", FakeProvider)
    monkeypatch.setattr(models, "OpenAIModel", FakeModel)

    settings = SimpleNamespace(openai_api_key="standard-key", openai_base_url="")
    models.resolve_model("openai/gpt-test", settings)

    assert captured["provider_kwargs"] == {"api_key": "standard-key"}


def test_glm_context_window_is_explicit() -> None:
    assert models.context_window("glm-5.3") == 524_288
    assert models.context_window("openai/glm-5.3") == 524_288
    assert models.context_window("openai/GLM-5.3") == 524_288


def test_unknown_model_keeps_default_context_window() -> None:
    assert models.context_window("openai/totally-unknown-model") == 200_000
