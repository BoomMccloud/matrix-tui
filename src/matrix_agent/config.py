from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "", "env_file": ".env"}

    matrix_homeserver: str = "https://matrix.org"
    matrix_user: str
    matrix_password: str
    llm_api_key: str
    llm_model: str = "openrouter/anthropic/claude-sonnet-4"
    podman_path: str = "podman"
    sandbox_image: str = "matrix-agent-sandbox:latest"
    command_timeout_seconds: int = 120
    max_agent_turns: int = 25
    screenshot_script: str = "/opt/playwright/screenshot.js"
