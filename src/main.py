import os
import sys
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")


def main():
    mode = os.getenv("MODE", "controller").lower()
    logger.info(f"Starting SimpleGSLB in [{mode}] mode")

    if mode == "controller":
        from controller import run

        run()

    else:
        logger.error(f"Unknown mode: {mode}")
        sys.exit(1)


if __name__ == "__main__":
    main()
