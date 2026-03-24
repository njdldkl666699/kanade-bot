from pydantic import BaseModel


class Config(BaseModel):
    apihz_api_url: str = "https://cn.apihz.cn/api"
    """接口盒子API URL"""
    apihz_id: str
    """接口盒子开发者ID"""
    apihz_key: str
    """接口盒子开发者Key"""
