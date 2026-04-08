from base64 import b64encode
from io import BytesIO
from pathlib import Path
from typing import Union


def f2s(file: Union[str, bytes, BytesIO, Path]) -> str:
    if isinstance(file, BytesIO):
        file = file.getvalue()
    if isinstance(file, bytes):
        file = f"base64://{b64encode(file).decode()}"
    elif isinstance(file, Path):
        file = file.resolve().as_uri()
    return file


print(f2s(Path("./宵崎奏Ciallo.webp")))
