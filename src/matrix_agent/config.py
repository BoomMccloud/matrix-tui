from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "", "env_file": ".env"}

    vps_ip: str = ""
    matrix_homeserver: str = "https://matrix.org"
    matrix_user: str
    matrix_password: str
    matrix_admin_user: str = ""
    matrix_admin_password: str = ""
    llm_api_key: str
    llm_model: str = "openrouter/anthropic/claude-haiku-4-5"
    podman_path: str = "podman"
    sandbox_image: str = "matrix-agent-sandbox:latest"
    command_timeout_seconds: int = 120
    coding_timeout_seconds: int = 1800
    max_agent_turns: int = 25
    screenshot_script: str = "/opt/playwright/screenshot.js"
    gemini_api_key: str = ""
    github_token: str = ""
    ipc_base_dir: str = "/tmp/sandbox-ipc"
