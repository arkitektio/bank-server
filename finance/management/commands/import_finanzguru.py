"""Preview (and optionally apply) a Finanzguru export from a local file.

The same code path as ``createFinanzguruImport`` / ``applyStatementImport``, for one-offs and for
checking a real export before a client uploads it::

    python manage.py import_finanzguru export.xlsx --org 3            # preview only
    python manage.py import_finanzguru export.xlsx --org 3 --apply
    python manage.py import_finanzguru export.xlsx --org 3 --apply --account DE12...=new --account "PayPal=skip"
"""

import json
from pathlib import Path

from authentikate.models import Organization
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from finance import models
from finance.imports import apply as importing


class Command(BaseCommand):
    help = "Preview (and with --apply, write) a Finanzguru transaction export into an organization's accounts."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("path", help="The export (.xlsx, or CSV with the same columns).")
        parser.add_argument("--org", required=True, help="The organization: its id or slug.")
        parser.add_argument("--apply", action="store_true", help="Write the import (default: preview only).")
        parser.add_argument(
            "--account", action="append", default=[], metavar="REFERENZKONTO=ID|new|skip", help="Where one account of the file goes (repeatable)."
        )

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        path = Path(options["path"])
        if not path.is_file():
            raise CommandError(f"No such file: {path}")
        org_filter = Q(slug=options["org"]) | (Q(id=int(options["org"])) if options["org"].isdigit() else Q())
        organization = Organization.objects.filter(org_filter).first()
        if organization is None:
            raise CommandError(f"No organization {options['org']!r}.")
        choices = {}
        for item in options["account"]:
            reference, _, target = item.rpartition("=")
            if not reference:
                raise CommandError(f"--account {item!r}: expected REFERENZKONTO=ID|new|skip.")
            choices[reference] = {"skip": True} if target == "skip" else {"account": None if target == "new" else int(target)}

        content = path.read_bytes()
        statement_import = models.StatementImport.objects.create(organization=organization, source=models.ImportSource.FINANZGURU, file_name=path.name)
        importing.preview(statement_import, content)
        if statement_import.status == models.ImportStatus.FAILED:
            raise CommandError(statement_import.error)
        if options["apply"]:
            try:
                importing.apply(statement_import.id, content, choices)
            except importing.StatementImportError as error:
                raise CommandError(str(error)) from error
            statement_import.refresh_from_db()
        self.stdout.write(json.dumps({"import": statement_import.id, "status": statement_import.status, **statement_import.report}, indent=2, ensure_ascii=False))
