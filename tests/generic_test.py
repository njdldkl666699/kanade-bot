from typing import Generic, TypeVar


T = TypeVar("T")


class Base(Generic[T]):
    def __init__(self, value: T):
        self.value = value


Inherit1 = Base[int]


class Inherit2(Base[str]):
    pass


print(Base)
print(Base.__name__)

print(Inherit1)
print(Inherit1.__name__)

print(Inherit2)
print(Inherit2.__name__)
