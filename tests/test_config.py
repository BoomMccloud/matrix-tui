from matrix_agent.config import Settings


def test_derive_from_vps_ip_success():
    """When vps_ip is set and matrix_homeserver/matrix_user are empty, they should be derived from vps_ip."""
    vps_ip = "1.2.3.4"
    settings = Settings(
        vps_ip=vps_ip,
        matrix_password="pass",
        llm_api_key="key"
    )
    assert settings.matrix_homeserver == f"http://{vps_ip}:8008"
    assert settings.matrix_user == f"@matrixbot:{vps_ip}"


def test_derive_from_vps_ip_no_overwrite():
    """When matrix_homeserver and matrix_user are already set, they should NOT be overwritten."""
    vps_ip = "1.2.3.4"
    custom_hs = "https://custom.hs"
    custom_user = "@custom:user"
    settings = Settings(
        vps_ip=vps_ip,
        matrix_homeserver=custom_hs,
        matrix_user=custom_user,
        matrix_password="pass",
        llm_api_key="key"
    )
    assert settings.matrix_homeserver == custom_hs
    assert settings.matrix_user == custom_user


def test_derive_from_vps_ip_empty_vps_ip():
    """When vps_ip is empty, derived fields should remain unchanged if they have values."""
    custom_hs = "https://example.com"
    custom_user = "@user:example.com"
    settings = Settings(
        vps_ip="",
        matrix_homeserver=custom_hs,
        matrix_user=custom_user,
        matrix_password="pass",
        llm_api_key="key"
    )
    assert settings.matrix_homeserver == custom_hs
    assert settings.matrix_user == custom_user


def test_derive_from_vps_ip_empty_vps_ip_fallback():
    """When vps_ip is empty and matrix_homeserver is empty, it should default to matrix.org."""
    settings = Settings(
        vps_ip="",
        matrix_homeserver="",
        matrix_password="pass",
        llm_api_key="key"
    )
    assert settings.matrix_homeserver == "https://matrix.org"
    assert settings.matrix_user == ""


def test_derive_from_vps_ip_partial_overwrite():
    """When only one of matrix_homeserver or matrix_user is set, the other should still be derived if vps_ip is set."""
    vps_ip = "1.2.3.4"
    custom_hs = "https://custom.hs"
    settings = Settings(
        vps_ip=vps_ip,
        matrix_homeserver=custom_hs,
        matrix_password="pass",
        llm_api_key="key"
    )
    assert settings.matrix_homeserver == custom_hs
    assert settings.matrix_user == f"@matrixbot:{vps_ip}"
