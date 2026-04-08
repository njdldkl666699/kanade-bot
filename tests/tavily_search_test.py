import os
import time

from dotenv import load_dotenv
import httpx

load_dotenv(".env")
load_dotenv(".env.prod")


def main():
    time_begin = time.time()
    response = httpx.post(
        "https://api.tavily.com/search",
        json={
            "query": "OpenClaw",
            "search_depth": "ultra-fast",
            "max_results": 10,
            # "country": "china", # Country parameter is not supported for fast or ultra-fast search_depth.
            "exclude_domains": ["cndn.net"],
        },
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.getenv('CHAT_TAVILY_API_KEY')}",
        },
    )
    time_end = time.time()
    print(f"Search completed in {time_end - time_begin:.2f} seconds")
    print(response)


if __name__ == "__main__":
    main()
