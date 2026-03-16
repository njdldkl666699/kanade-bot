import httpx


def main():
    response = httpx.get(
        "https://cn.apihz.cn/api/zici/mosi.php",
        params={
            "id": "10006249",
            "key": "b98d38bf7b5accbf9d1dfa9afea9ac7c",
            "type": 0,
            "words": "你好",
        },
    )
    print(response.text)
    print(response.json())


if __name__ == "__main__":
    main()
