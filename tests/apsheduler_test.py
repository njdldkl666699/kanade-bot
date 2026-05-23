import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler


async def test():
    print("Hello, World!")


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    scheduler = AsyncIOScheduler(event_loop=loop)
    scheduler.add_job(test, "interval", seconds=1)
    scheduler.start()
    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
