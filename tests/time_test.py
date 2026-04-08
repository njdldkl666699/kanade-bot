from datetime import datetime


def get_datetime_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


print(get_datetime_now())
