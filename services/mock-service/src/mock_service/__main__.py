"""Mock service process entry point."""

import os

import uvicorn


def main() -> None:
    """Start locally by default; Compose opts into the container interface."""
    uvicorn.run(
        "mock_service.main:app",
        host=os.environ.get("MOCK_HOST", "127.0.0.1"),
        port=8082,
    )


if __name__ == "__main__":
    main()
