from django.db import models
from django.core.exceptions import ValidationError
import re


def validate_store_path(value: str) -> None:
    """Validate that the value is a supported object-store URI."""
    pattern = r"^(seaweed|s3)://[^/]+/.+"
    if not re.match(pattern, value):
        raise ValidationError(
            "Invalid store path format. Expected seaweed://bucket/object_key",
            code="invalid",
        )


class StorePathField(models.CharField):
    """A CharField to store SeaweedFS-backed object paths."""

    description = "CharField to store object-store paths with validation"

    def __init__(self, *args, **kwargs) -> None:
        """Initialize the field with a default max_length of 500."""
        kwargs["max_length"] = kwargs.get("max_length", 500)
        validators = list(kwargs.get("validators", []))
        validators.append(validate_store_path)
        kwargs["validators"] = validators

        super().__init__(*args, **kwargs)


S3Field = StorePathField
