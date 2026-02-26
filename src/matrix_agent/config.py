from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "", "env_file": ".env", "extra": "ignore"}

    vps_ip: str = ""
    matrix_homeserver: str = ""
    matrix_user: str = ""
    matrix_password: str

    @model_validator(mode="after")
    def derive_from_vps_ip(self) -> "Settings":
        if self.vps_ip:
            if not self.matrix_homeserver:
                self.matrix_homeserver = f"http://{self.vps_ip}:8008"
            if not self.matrix_user:
                self.matrix_user = f"@matrixbot:{self.vps_ip}"
        elif not self.matrix_homeserver:
            self.matrix_homeserver = "https://matrix.org"
        return self
    llm_api_key: str
    llm_api_base: str = ""
    llm_model: str = "openrouter/anthropic/claude-haiku-4-5"
    podman_path: str = "podman"
    sandbox_image: str = "matrix-agent-sandbox:latest"
    command_timeout_seconds: int = 120
    coding_timeout_seconds: int = 1800
    max_agent_turns: int = 25
    screenshot_script: str = "/opt/playwright/screenshot.js"
    gemini_api_key: str = ""
    dashscope_api_key: str = ""
    github_token: str = ""
    ipc_base_dir: str = "/tmp/sandbox-ipc"
