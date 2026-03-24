from pydantic import BaseModel


class Config(BaseModel):
    api60s_base_url: str = "https://60s.viki.moe"
    """60s API Base URL"""
