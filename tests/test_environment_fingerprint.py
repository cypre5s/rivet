"""验证环境指纹不包含凭据字段值。"""

from scripts.environment_fingerprint import collect_environment_fingerprint


def test_environment_fingerprint_only_records_key_presence() -> None:
    fingerprint = collect_environment_fingerprint()

    assert "deepseek_api_key_configured" in fingerprint
    assert "deepseek_api_key" not in fingerprint
    assert isinstance(fingerprint["deepseek_api_key_configured"], bool)
