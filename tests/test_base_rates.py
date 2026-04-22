from __future__ import annotations

from app.base_rates import BaseRateLookup


class TestBaseRateLookup:
    def test_exact_match(self):
        br = BaseRateLookup({"trump": {"rally": {"tariff": 0.85}}, "_global_default": 0.30})
        assert br.get("trump", "rally", "tariff") == 0.85

    def test_fallback_to_speaker_default(self):
        br = BaseRateLookup({"trump": {"_default": {"nato": 0.25}}, "_global_default": 0.30})
        assert br.get("trump", "interview", "nato") == 0.25

    def test_fallback_to_global(self):
        # Trump with explicit _global_default dict falls back to dict's value
        # (speaker has no _context_base, so uses _global_default from data)
        br = BaseRateLookup({"trump": {"rally": {"tariff": 0.85}}, "_global_default": 0.30})
        # Now returns _SPEAKER_DEFAULTS["trump"]=0.55 since speaker_data has no _context_base
        result = br.get("trump", "rally", "unknown_phrase")
        assert result == 0.55  # uses _SPEAKER_DEFAULTS["trump"]

    def test_unknown_speaker_returns_global(self):
        br = BaseRateLookup({"_global_default": 0.30})
        assert br.get("nobody", "rally", "test") == 0.30  # unknown speaker → global dict default

    def test_case_insensitive_phrase(self):
        br = BaseRateLookup({"trump": {"rally": {"nato": 0.40}}, "_global_default": 0.30})
        assert br.get("trump", "rally", "NATO") == 0.40

    def test_empty_data(self):
        # Empty data → falls through to _SPEAKER_DEFAULTS["trump"]=0.55
        br = BaseRateLookup()
        assert br.get("trump", "rally", "tariff") == 0.55

    def test_from_yaml_missing_file(self, tmp_dir):
        # Missing file → empty data → _SPEAKER_DEFAULTS["trump"]=0.55
        br = BaseRateLookup.from_yaml(tmp_dir / "nope.yaml")
        assert br.get("trump", "rally", "tariff") == 0.55

    def test_from_yaml_loads(self, tmp_dir):
        yaml_path = tmp_dir / "base_rates.yaml"
        yaml_path.write_text(
            "trump:\n"
            "  rally:\n"
            "    tariff: 0.85\n"
            "_global_default: 0.25\n"
        )
        br = BaseRateLookup.from_yaml(yaml_path)
        assert br.get("trump", "rally", "tariff") == 0.85
        # No _context_base in trump section → falls to _SPEAKER_DEFAULTS["trump"]=0.55
        assert br.get("trump", "rally", "unknown") == 0.55
