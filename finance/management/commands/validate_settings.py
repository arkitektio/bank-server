"""Validate this service's configuration without starting the app.

Re-loads the typed settings schema (the same one Django builds at boot) from
``config.yaml`` + environment variables, then prints the fully resolved
configuration as a tree with secrets redacted. Exits non-zero with a
field-by-field report when the configuration is invalid.

    python manage.py validate_settings

Honors ``ARKITEKT_CONFIG_FILE`` to point at an alternate YAML file.
"""

from __future__ import annotations

import os

from django.core.management.base import BaseCommand
from pydantic import ValidationError
from rich.console import Console
from rich.tree import Tree

from bank_server.configuration import Settings, _DEFAULT_CONFIG

# Leaf keys whose values are secrets and must never be printed in the clear.
SECRET_HINTS = ("password", "secret_key", "secret", "private_key", "access_key")


def _is_secret(key: object) -> bool:
    name = str(key).lower()
    return any(hint in name for hint in SECRET_HINTS)


def _mask(value: object) -> str:
    if isinstance(value, str):
        return f"**** (len={len(value)})"
    return "****"


def _add(tree: Tree, data: object, *, secret: bool = False) -> None:
    """Recursively render ``data`` (a ``model_dump()`` result) into ``tree``.

    ``secret`` masks every leaf below this point (a secret-named block); leaves
    whose own key looks secret are masked individually.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            key_secret = secret or _is_secret(key)
            if isinstance(value, (dict, list)):
                branch = tree.add(f"[bold cyan]{key}[/bold cyan]")
                _add(branch, value, secret=key_secret)
            else:
                shown = _mask(value) if key_secret else repr(value)
                tree.add(f"[cyan]{key}[/cyan]: {shown}")
    elif isinstance(data, list):
        for index, item in enumerate(data):
            if isinstance(item, (dict, list)):
                branch = tree.add(f"[bold cyan]item {index}[/bold cyan]")
                _add(branch, item, secret=secret)
            else:
                tree.add(_mask(item) if secret else repr(item))
    else:
        tree.add(_mask(data) if secret else repr(data))


class Command(BaseCommand):
    help = "Validate the service configuration (YAML + env) and print the resolved, redacted settings."
    # A config validator needs no model/URL/DB system checks (and they may fail
    # independently of config); skip them so only configuration is exercised.
    requires_system_checks: list = []

    def handle(self, *args, **options) -> None:
        console = Console()
        path = os.environ.get("ARKITEKT_CONFIG_FILE", _DEFAULT_CONFIG)
        try:
            settings = Settings()
        except ValidationError as exc:
            console.print(f"[bold red]Invalid configuration[/bold red] (source: {path})")
            for err in exc.errors():
                loc = ".".join(str(part) for part in err["loc"])
                console.print(f"  [red]{loc}[/red]: {err['msg']}")
            raise SystemExit(1)

        tree = Tree(f"[bold green]Configuration valid[/bold green] (source: {path})")
        _add(tree, settings.model_dump())
        console.print(tree)
