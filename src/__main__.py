"""Entry point for running the MCP Gateway."""

import argparse
import logging
import sys

import uvicorn

from .config import load_config
from .server import create_app


def main():
    parser = argparse.ArgumentParser(description="MCP Gateway Server")
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Override host binding",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override port",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    # Apply CLI overrides
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port

    # Configure logging
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    app = create_app(config)

    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
    )


if __name__ == "__main__":
    main()
