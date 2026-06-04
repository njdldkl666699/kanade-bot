from loguru import logger
import rpyc


client = rpyc.connect("localhost", 39831)

results = client.root.query(
    "宵崎奏生日什么时候",
    n_results=5,
    threshold=0.8,
)


logger.info(f"查询结果: {results}")
