"""Run both external (8443) and internal (9090) uvicorn servers."""

import asyncio

import uvicorn


async def main():
    external = uvicorn.Server(
        uvicorn.Config("src.app:app", host="0.0.0.0", port=8443, log_level="info"),
    )
    internal = uvicorn.Server(
        uvicorn.Config("src.app:internal_app", host="0.0.0.0", port=9090, log_level="info"),
    )
    await asyncio.gather(external.serve(), internal.serve())


if __name__ == "__main__":
    asyncio.run(main())
