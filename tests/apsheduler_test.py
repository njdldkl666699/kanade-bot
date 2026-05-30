import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()


class TestScheduler:
    @classmethod
    def task(cls):
        print("Task executed!")

    @classmethod
    def start(cls):
        scheduler.add_job(cls.task, "interval", seconds=1)
        scheduler.start()


async def test():
    print("Hello, World!")


async def main():
    TestScheduler.start()
    await asyncio.sleep(5)  # 让调度器有时间执行任务


asyncio.run(main())
