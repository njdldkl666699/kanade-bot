from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import Mapping, Self
import hashlib
from pydantic import BaseModel, Field, model_validator
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class Base(BaseModel):
    model_config = {
        "validate_assignment": True,
    }


class Address(Base):
    street: str
    city: str
    zip_code: str


class UserInfo(Base):
    name: str
    age: int
    address: Address

    @model_validator(mode="after")
    def after_validation(self) -> Self:
        print("After validation: ", self)
        # 写入配置文件
        Path("test.json").write_text(self.model_dump_json(indent=2, ensure_ascii=False))
        return self


def md5(file_path: Path):
    return hashlib.md5(file_path.read_bytes()).hexdigest()


class ObservableModel(BaseModel):
    """一个可观察的配置模型，当配置文件被修改时会自动重新加载"""

    model_config = {
        "validate_assignment": True,
    }

    config_file_path: Path = Field(exclude=True)

    file_hash: str = Field(default="", exclude=True)

    @model_validator(mode="after")
    def save_config_after_validation(self) -> Self:
        new_hash = md5(self.config_file_path)
        if new_hash == self.file_hash:
            return self

        self.file_hash = new_hash
        self.config_file_path.write_text(self.model_dump_json(indent=2, ensure_ascii=False))
        return self


class TestConfig(ObservableModel):
    name: str
    age: int
    address: Address


@dataclass
class UserInfoWrapper:
    info: UserInfo


class MyHandler(FileSystemEventHandler):
    def __init__(self, wrapper: UserInfoWrapper):
        super().__init__()
        self.wrapper = wrapper
        self.file_hash = md5(Path("test.json"))

    # 当文件或文件夹被修改时触发
    def on_modified(self, event):
        print(f"[修改] {event}")
        if event.src_path.endswith("test.json"):
            new_hash = md5(Path("test.json"))
            if new_hash != self.file_hash:
                self.file_hash = new_hash
                new_config = UserInfo.model_validate_json(Path("test.json").read_text())
                self.wrapper.info = new_config
                print("Updated config: ", self.wrapper.info)


class Car(BaseModel):
    make: str
    model: str
    year: int

    model_config = {
        "validate_assignment": True,
    }


class CarUser(BaseModel):
    name: str
    car: Car

    model_config = {
        "validate_assignment": True,
    }

    @model_validator(mode="after")
    def after_validation(self) -> Self:
        print("After validation: ", self)
        return self


def main1():
    data = {
        "name": "Alice",
        "age": 30,
        "address": {"street": "123 Main St", "city": "Wonderland", "zip_code": "12345"},
    }
    info = UserInfo(**data)
    Path("test.json").write_text(info.model_dump_json(indent=2, ensure_ascii=False))

    observer = Observer()
    handler = MyHandler(UserInfoWrapper(info=info))
    observer.schedule(handler, path=".")
    observer.start()

    # test = TestConfig.model_validate_json(Path("test.json").read_text())

    print("________")
    info.age = 18

    sleep(1)


def main2():
    car_user = CarUser(name="Bob", car=Car(make="Toyota", model="Camry", year=2020))
    print(car_user)
    car_user.car.year = 2021
    print(car_user)


class JSONModel(BaseModel):
    my_set: set[int]
    my_tuple: tuple[str, int]
    my_dict: dict[str, int]

    model_config = {
        "validate_assignment": True,
    }


def main3():
    data = {
        "my_set": [1, 2, 3],
        "my_tuple": ["hello", 42],
        "my_dict": {"a": 1, "b": 2},
    }
    model = JSONModel(**data)
    if isinstance(model, Mapping):
        print("yes")
    # model_str = model.model_dump_json(indent=2, ensure_ascii=False)
    # model2 = JSONModel.model_validate_json(model_str)
    # for k, v in model2:
    #     if isinstance(v, list):
    #         print(k, type(v), set(v))


if __name__ == "__main__":
    # main2()
    main3()
