from __future__ import annotations

from app.signals import SignalStore, SignalModifiers, MODIFIER_MIN, MODIFIER_MAX


class TestSignalStore:
    def test_default_modifiers(self):
        store = SignalStore()
        m = store.get("tariff")
        assert m.news_pressure == 1.0
        assert m.x_buzz == 1.0

    def test_loaded_values(self):
        store = SignalStore()
        store._lookup["tariff"] = SignalModifiers(news_pressure=1.2, x_buzz=1.4)
        m = store.get("tariff")
        assert m.news_pressure == 1.2
        assert m.x_buzz == 1.4

    def test_case_insensitive(self):
        store = SignalStore()
        store._lookup["tariff"] = SignalModifiers(news_pressure=1.3, x_buzz=1.1)
        m = store.get("Tariff")
        assert m.news_pressure == 1.3

    def test_from_yaml(self, tmp_dir):
        # news_pressure is clamped to MODIFIER_MAX (currently 1.0 — LLM boost neutralised).
        # Values above MODIFIER_MAX are clamped; values below 1.0 (suppressions) pass through.
        yaml_path = tmp_dir / "signals.yaml"
        yaml_path.write_text(
            "signals:\n"
            '  - phrase: "tariff"\n'
            "    news_pressure: 1.2\n"
            "    x_buzz: 0.8\n"
        )
        store = SignalStore.from_yaml(yaml_path)
        m = store.get("tariff")
        assert m.news_pressure == MODIFIER_MAX   # 1.2 clamped to MODIFIER_MAX (1.0)
        assert m.x_buzz == 0.8                   # suppression passes through unchanged

    def test_clamping_high(self, tmp_dir):
        yaml_path = tmp_dir / "signals.yaml"
        yaml_path.write_text(
            "signals:\n"
            '  - phrase: "extreme"\n'
            "    news_pressure: 5.0\n"
            "    x_buzz: 3.0\n"
        )
        store = SignalStore.from_yaml(yaml_path)
        m = store.get("extreme")
        assert m.news_pressure == MODIFIER_MAX
        assert m.x_buzz == MODIFIER_MAX

    def test_clamping_low(self, tmp_dir):
        yaml_path = tmp_dir / "signals.yaml"
        yaml_path.write_text(
            "signals:\n"
            '  - phrase: "quiet"\n'
            "    news_pressure: 0.1\n"
            "    x_buzz: 0.2\n"
        )
        store = SignalStore.from_yaml(yaml_path)
        m = store.get("quiet")
        assert m.news_pressure == MODIFIER_MIN
        assert m.x_buzz == MODIFIER_MIN

    def test_missing_file_returns_defaults(self, tmp_dir):
        store = SignalStore.from_yaml(tmp_dir / "nope.yaml")
        m = store.get("anything")
        assert m.news_pressure == 1.0
        assert m.x_buzz == 1.0

    def test_source_story_hashes_from_yaml(self, tmp_dir):
        ha, hb = "a" * 16, "b" * 16
        yaml_path = tmp_dir / "signals.yaml"
        yaml_path.write_text(
            "signals:\n"
            '  - phrase: "tariff"\n'
            "    news_pressure: 1.1\n"
            "    x_buzz: 1.0\n"
            f'    source_story_hashes: ["{ha}", "{hb}"]\n'
            f'    news_story_hashes: ["{"c" * 16}"]\n'
        )
        store = SignalStore.from_yaml(yaml_path)
        m = store.get("tariff")
        assert m.source_story_hashes == (ha, hb)
        assert m.news_story_hashes == ("c" * 16,)
