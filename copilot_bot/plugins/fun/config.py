from pydantic import BaseModel


class Config(BaseModel):
    fun_music_lists_path: str
    fun_music_list_link = str
