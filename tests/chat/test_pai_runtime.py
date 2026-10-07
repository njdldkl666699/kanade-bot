"""pai_runtime 的 /models 元数据获取（context_window / max_output_tokens）与 profile 填充"""

from kanade_bot.utils import pai_runtime
from kanade_bot.utils.schema import BaseAgentConfig, ProviderConfig

PROVIDER = ProviderConfig(base_url="https://api.example.com", api_key="sk-test")


def setup_function(_):
    # get_model / _fetch_model_metadata 均有按 (client, model) 的缓存，逐用例清空避免串扰
    pai_runtime._model_cache.clear()
    pai_runtime._models_metadata_cache.clear()


class TestExtractModelMetadata:
    def test_match(self):
        payload = {
            "data": [
                {"id": "deepseek-flash", "context_window": 1048576, "max_output_tokens": 393216}
            ]
        }
        assert pai_runtime._extract_model_metadata(payload, "deepseek-flash") == (1048576, 393216)

    def test_other_model_ignored(self):
        payload = {"data": [{"id": "deepseek-reasoner", "context_window": 65536}]}
        assert pai_runtime._extract_model_metadata(payload, "deepseek-flash") == (None, None)

    def test_missing_fields(self):
        payload = {"data": [{"id": "deepseek-flash"}]}
        assert pai_runtime._extract_model_metadata(payload, "deepseek-flash") == (None, None)

    def test_non_positive_values(self):
        payload = {"data": [{"id": "m", "context_window": 0, "max_output_tokens": -1}]}
        assert pai_runtime._extract_model_metadata(payload, "m") == (None, None)

    def test_partial_fields(self):
        payload = {"data": [{"id": "m", "context_window": 128000}]}
        assert pai_runtime._extract_model_metadata(payload, "m") == (128000, None)

    def test_non_dict_entries(self):
        payload = {
            "data": ["oops", None, {"id": "m", "context_window": 128, "max_output_tokens": 64}]
        }
        assert pai_runtime._extract_model_metadata(payload, "m") == (128, 64)

    def test_openrouter_style_fields(self):
        # SenseNova 等 OpenRouter 风格：context_length / max_output_length
        payload = {
            "data": [
                {
                    "id": "deepseek-v4-flash",
                    "context_length": 1048576,
                    "max_output_length": 65536,
                }
            ]
        }
        assert pai_runtime._extract_model_metadata(payload, "deepseek-v4-flash") == (
            1048576,
            65536,
        )

    def test_deepseek_style_wins_when_both_present(self):
        payload = {
            "data": [
                {
                    "id": "m",
                    "context_window": 111,
                    "context_length": 222,
                    "max_output_tokens": 333,
                    "max_output_length": 444,
                }
            ]
        }
        assert pai_runtime._extract_model_metadata(payload, "m") == (111, 333)


class TestFetchModelMetadata:
    def test_no_base_url_returns_none(self):
        provider = ProviderConfig(api_key="sk-test")
        assert pai_runtime._fetch_model_metadata(provider, "any") == (None, None)

    def test_result_is_cached(self):
        pai_runtime._models_metadata_cache[(pai_runtime._client_key(PROVIDER), "m")] = (111, 222)
        assert pai_runtime._fetch_model_metadata(PROVIDER, "m") == (111, 222)


class TestGetModelProfile:
    def test_config_window_takes_priority(self, monkeypatch):
        # 显式配置时不应发 /models 查询
        def _boom(_p, _m):
            raise AssertionError("配置了 context_window 时不应查询 /models")

        monkeypatch.setattr(pai_runtime, "_fetch_model_metadata", _boom)
        model = pai_runtime.get_model(
            BaseAgentConfig(model="any-model", provider=PROVIDER, context_window=131072)
        )
        assert model.context_window == 131072

    def test_config_window_without_provider(self, monkeypatch):
        monkeypatch.setattr(pai_runtime, "_fetch_model_metadata", lambda _p, _m: (None, None))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        model = pai_runtime.get_model(BaseAgentConfig(model="gpt-4o", context_window=128000))
        assert model.context_window == 128000

    def test_fetched_window_overrides_snapshot(self, monkeypatch):
        monkeypatch.setattr(pai_runtime, "_fetch_model_metadata", lambda _p, _m: (1048576, None))
        model = pai_runtime.get_model(BaseAgentConfig(model="deepseek-chat", provider=PROVIDER))
        assert model.context_window == 1048576

    def test_fetch_failure_falls_back_to_snapshot(self, monkeypatch):
        monkeypatch.setattr(pai_runtime, "_fetch_model_metadata", lambda _p, _m: (None, None))
        provider = ProviderConfig(base_url="https://api.deepseek.com", api_key="sk-test")
        model = pai_runtime.get_model(BaseAgentConfig(model="deepseek-chat", provider=provider))
        # 快照填充兜底（deepseek-chat 条目为 64000）
        assert model.context_window == 64000


class TestBuildModelSettings:
    def test_explicit_config_takes_priority(self, monkeypatch):
        monkeypatch.setattr(pai_runtime, "_fetch_model_metadata", lambda _p, _m: (None, 999999))
        settings = pai_runtime.build_model_settings(
            BaseAgentConfig(model="m", provider=PROVIDER, max_output_tokens=4096)
        )
        assert settings["max_tokens"] == 4096

    def test_fetched_max_output_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(pai_runtime, "_fetch_model_metadata", lambda _p, _m: (None, 393216))
        settings = pai_runtime.build_model_settings(BaseAgentConfig(model="m", provider=PROVIDER))
        assert settings["max_tokens"] == 393216

    def test_no_provider_no_limit(self, monkeypatch):
        monkeypatch.setattr(pai_runtime, "_fetch_model_metadata", lambda _p, _m: (None, 999))
        settings = pai_runtime.build_model_settings(BaseAgentConfig(model="gpt-4o"))
        assert "max_tokens" not in settings

    def test_endpoint_without_field_no_limit(self, monkeypatch):
        monkeypatch.setattr(pai_runtime, "_fetch_model_metadata", lambda _p, _m: (None, None))
        settings = pai_runtime.build_model_settings(BaseAgentConfig(model="m", provider=PROVIDER))
        assert "max_tokens" not in settings
