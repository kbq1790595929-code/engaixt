import logging
from datetime import datetime
from pathlib import Path

_logger: logging.Logger | None = None
_log_callback: object = None


def _format_time():
    return datetime.now().strftime("%H:%M:%S")


class _CallbackHandler(logging.Handler):
    def emit(self, record):
        if _log_callback:
            try:
                _log_callback(record.levelno, self.format(record))
            except Exception:
                pass


def setup_logger(log_dir: Path | None = None, callback: object = None):
    global _logger, _log_callback
    _log_callback = callback

    _logger = logging.getLogger("game_translator")
    _logger.setLevel(logging.DEBUG)
    _logger.handlers.clear()

    fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")

    if callback:
        cb = _CallbackHandler()
        cb.setLevel(logging.INFO)
        cb.setFormatter(fmt)
        _logger.addHandler(cb)

    if log_dir:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "translate.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        _logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(fmt)
    _logger.addHandler(ch)


def get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        setup_logger()
    return _logger


def info(msg: str):
    get_logger().info(msg)


def debug(msg: str):
    get_logger().debug(msg)


def warning(msg: str):
    get_logger().warning(msg)


def error(msg: str):
    get_logger().error(msg)


def set_callback(callback: object):
    global _log_callback
    _log_callback = callback
    if _logger:
        for h in _logger.handlers:
            if isinstance(h, _CallbackHandler):
                _logger.removeHandler(h)
        fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
        cb = _CallbackHandler()
        cb.setLevel(logging.INFO)
        cb.setFormatter(fmt)
        _logger.addHandler(cb)
