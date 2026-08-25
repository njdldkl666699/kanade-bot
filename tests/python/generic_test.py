from pydantic import BaseModel, TypeAdapter


class Base[T]:
    cls_value: T | None = None

    def __init__(self, value: T):
        self.value = value

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.cls_value = None


Inherit1 = Base[int]


class Inherit2(Base[str]):
    pass


# print(Base)
# print(Base.__name__)

# print(Inherit1)
# print(Inherit1.__name__)

# print(Inherit2)
# print(Inherit2.__name__)

# Inherit1.cls_value = 123
# print(Base.cls_value)
# print(Inherit1.cls_value)
# print(Inherit2.cls_value)


class Car(BaseModel):
    brand: str
    model: str
    year: int

    def __str__(self):
        return f"{self.brand} {self.model} ({self.year})"


class MyModel[T](BaseModel):
    values: dict[str, T]


class MyModelModel[T]:
    def __init__(self, car: T, car_type: type[T]):
        json = '{"values": {"mycar": {"brand": "Toyota", "model": "Corolla", "year": 2020}}}'
        self._model = MyModel[car_type].model_validate_json(json)

    def get(self) -> T:
        return self._model.values["mycar"]


mm = MyModelModel(Car(brand="Toyota", model="Corolla", year=2020), Car)

print(mm.get())

# car = MyModel[Car](values={"mycar": Car(brand="Toyota", model="Corolla", year=2020)})
# print(str(car.values["mycar"]))

# json_str = car.model_dump_json()

# car2 = MyModel[Car].model_validate_json(json_str)
# print(str(car2.values["mycar"]))
