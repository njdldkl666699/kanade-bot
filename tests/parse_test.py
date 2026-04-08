import httpx


def main():
    response = httpx.get(
        "https://cn.apihz.cn/api/zici/mosi.php",
        params={
            "id": "xxxxx",
            "key": "xxxxxxxxxxxxxxxxxxx",
            "type": 0,
            "words": "你好",
        },
    )
    print(response.text)
    print(response.json())


def test_baidu_search():
    response = httpx.get(
        "https://cn.apihz.cn/api/wangzhan/soubaidu.php",
        params={
            "id": "xxxxxx",
            "key": "xxxxxxxxxxxxxxxxx",
            "words": "OpenClaw",
            "page": 1,
        },
    )
    print(response.json())


if __name__ == "__main__":
    test_baidu_search()
