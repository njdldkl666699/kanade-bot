from pydantic import BaseModel


class Config(BaseModel):
    help_link: str
