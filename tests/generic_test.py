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

Inherit1.cls_value = 123
print(Base.cls_value)
print(Inherit1.cls_value)
print(Inherit2.cls_value)
