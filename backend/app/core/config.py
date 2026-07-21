from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from app.core.env import env_flag, load_local_env_file


class Settings(BaseModel):
    app_name: str = "Annual Statistics Review API"
    api_prefix: str = "/api"
    allowed_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    upload_dir: Path = Path(__file__).resolve().parents[2] / "uploads"
    max_upload_bytes: int = 100 * 1024 * 1024
    auto_import_include_llm: bool = False


@lru_cache
def get_settings() -> Settings:
    load_local_env_file()
    return Settings(
        auto_import_include_llm=env_flag("AUTO_IMPORT_INCLUDE_LLM", default=False),
    )
