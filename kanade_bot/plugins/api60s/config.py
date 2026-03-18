from pydantic import BaseModel


class Config(BaseModel):
    api60s_base_url: str
