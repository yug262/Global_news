import asyncio
from app.workers.monitor import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nWorker shutting down...")
