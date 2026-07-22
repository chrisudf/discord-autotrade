"""loguru logger: split files + daily rotation

import 无副作用:模块只 re-export loguru 的 `logger`(带 loguru 默认 sink)。
老 src/utils/logger.py 的 import-time sink 配置原样搬进 setup_logging(log_dir),
由 app/main 或脚本显式调用。
"""
import sys
from pathlib import Path

from loguru import logger


def setup_logging(log_dir: Path) -> None:
    """配置 sink(老 src/utils/logger.py 的 import-time 逻辑,行为逐字保留)。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(exist_ok=True)
    (log_dir / "errors").mkdir(exist_ok=True)

    logger.remove()
    logger.add(
        sys.stdout,
        level="INFO",
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level}</level> | {message}",
    )
    logger.add(
        str(log_dir / "app_{time:YYYY-MM-DD}.log"),
        rotation="1 day",
        retention="30 days",
        level="DEBUG",
    )
    logger.add(
        str(log_dir / "errors" / "error_{time:YYYY-MM-DD}.log"),
        rotation="1 day",
        retention="60 days",
        level="ERROR",
    )
