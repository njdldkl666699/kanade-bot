from pydantic import BaseModel


class Config(BaseModel):
    apihz_api_url: str
    apihz_id: str
    apihz_key: str
