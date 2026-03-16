from pydantic import BaseModel


class Config(BaseModel):
    apihz_api_url: str
    apihz_id: int
    apihz_key: str
