"""Local Web workbench for Mini Claude Code."""


def create_app(*args, **kwargs):
    from .server import create_app as _create_app
    return _create_app(*args, **kwargs)


def run_web(*args, **kwargs):
    from .server import run_web as _run_web
    return _run_web(*args, **kwargs)


__all__ = ["create_app", "run_web"]
