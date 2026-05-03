"""worker.py — RQ worker entrypoint."""

import os

from redis import Redis
from rq import Queue, Worker


def main():
    conn = Redis.from_url(os.environ["REDIS_URL"])
    queues = [Queue("deploys", connection=conn)]
    worker = Worker(queues, connection=conn)
    print("[worker] waiting for jobs in queue 'deploys'...")
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
