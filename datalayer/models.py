import logging
from pathlib import PurePosixPath
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar
from uuid import uuid4

from django.conf import settings
from django.db import models
from polymorphic.models import PolymorphicModel
from datalayer import base_models
from datalayer.datalayer import AccessGrant, Datalayer

if TYPE_CHECKING:
    from types_boto3_s3.type_defs import FileobjTypeDef


logger = logging.getLogger(__name__)


def get_default_upload_token() -> str:
    """Return the default opaque token used sfor storage keys."""
    return uuid4().hex


def build_opaque_storage_key(original_file_name: str, generator: Callable[[], str] = get_default_upload_token) -> str:
    """Build a fully opaque storage key without sembsedding filename metadata."""
    del original_file_name
    return generator()


class DatalayerStore(PolymorphicModel):
    """An object stored behind the S3-backed datalayer."""

    objects: models.Manager["DatalayerStore"]  # type: ignore[assignment]

    # The store lifecycle below (`bucket_key`, `orphaned_at`, `purge_bytes`, `delete`) is
    # vendored from elektro's (mikro's) datalayer/models.py, cut down to single-object stores:
    # bank only ever stores uploaded files (a statement export to import).

    #: The logical datalayer bucket this kind of store lives in -- the key
    #: ``Datalayer.get_bucket_config`` takes, and the value written to the ``bucket`` column.
    #: A ClassVar because it is a property of the *type*, not of a row.
    bucket_key: ClassVar[str] = ""

    organization = models.ForeignKey(
        "authentikate.Organization",
        on_delete=models.CASCADE,
        help_text="The organization this store belongs to.",
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The user who requested the upload grant, whose storage quota the bytes count against. Null for stores written before this was recorded.",
    )
    path = models.CharField(max_length=1000, null=True, blank=True, help_text="The object-store URI of the file", unique=True)
    key = models.CharField(max_length=1000, help_text="The object key/path within the datalayer bucket.")
    bucket = models.CharField(max_length=1000, help_text="The datalayer bucket/service this store belongs to.")
    original_file_name = models.CharField(max_length=1000, null=True, blank=True, help_text="The original client-provided file name.")
    content_type = models.CharField(max_length=255, null=True, blank=True, help_text="The client-provided content type for the uploaded file.")
    populated = models.BooleanField(default=False, help_text="Whether the store has been populated with a valid path and is ready for use.")
    size_bytes = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="How many bytes this store actually holds, measured when the upload was finished. Null while an upload is unfinished, or for stores written before this was recorded",
    )
    orphaned_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "When the last data row referencing this store was deleted, or null while it is still in use. A *candidate* for garbage collection, not an authority: "
            "`purge_orphaned_stores` re-checks for referrers before deleting anything, and clears this again if the store was re-attached in the meantime"
        ),
    )

    def build_store_path(self, datalayer: Datalayer | None = None) -> str:
        """Return the canonical object-store URI for this store."""
        layer = datalayer or Datalayer()
        return layer.build_store_path(self.bucket, self.key)

    def grant_read_access(self, datalayer: Datalayer, host: str | None = None) -> AccessGrant:
        """Return temporary credentials for reading this store."""
        del host
        return datalayer.generate_file_read_url(self.bucket, self.key, store_id=str(self.pk))

    def fill_info(self, datalayer: Datalayer | None = None) -> None:
        """Finalize the store after a successful upload."""
        raise NotImplementedError("Subclasses must implement fill_info()")

    def purge_bytes(self, datalayer: Datalayer | None = None) -> int:
        """Delete this store's object from S3, and return how many went.

        Idempotent, so a retry after a partial failure is safe -- deleting an absent key is a no-op.
        """
        layer = datalayer or Datalayer()
        layer.delete_object(self.bucket, self.key)
        return 1

    def measure_bytes(self, datalayer: Datalayer | None = None) -> int:
        """Return how many bytes this store actually occupies in S3 (a HEAD of its one key)."""
        layer = datalayer or Datalayer()
        return layer.get_object_size(self.bucket, layer.build_object_key(self.bucket, self.key))

    def measured_size(self, datalayer: Datalayer | None = None) -> int | None:
        """This store's size on disk, or ``None`` if it could not be read. Never raises.

        The non-fatal wrapper around :meth:`measure_bytes`, and the one every ``fill_info``
        calls. By the time a store is finalized its bytes are already in the bucket, so
        refusing the upload over a failed accounting read would lose the data to protect a
        number. A store whose size cannot be measured is still a finished store; ``size_bytes``
        stays null and says exactly that.
        """
        try:
            return self.measure_bytes(datalayer)
        except Exception:
            logger.warning("Could not measure the bytes held by %s store %s; leaving size_bytes unset.", self.bucket, self.pk, exc_info=True)
            return None

    def delete(self, *args, **kwargs) -> tuple[int, dict[str, int]]:
        """Delete the remote objects, then the row.

        **This purges immediately and ignores the grace period.** It is the "I mean it, now"
        path; the ordinary route is to let a data-row deletion flag the store and let
        `purge_orphaned_stores` collect it.

        Bytes first, then the row, deliberately: bytes gone with the row still present is
        recoverable by re-running the sweep, while the row gone with bytes left is a leak
        nothing points at any more. A failure therefore propagates rather than being logged
        and swallowed -- the old behaviour dropped the row anyway and left the bytes orphaned
        with no record of them.
        """
        self.purge_bytes()
        return super().delete(*args, **kwargs)

    def get_upload_file_name(self) -> str:
        """Return the client-visible filename to use in multipart uploads."""
        if self.original_file_name:
            return PurePosixPath(self.original_file_name).name

        return self.key.rsplit("/", 1)[-1]


class BigFileStore(DatalayerStore):
    """A large file stored behind the S3-backed datalayer."""

    objects: models.Manager["BigFileStore"]  # type: ignore[assignment]

    bucket_key: ClassVar[str] = "bigfile"

    def grant_read_access(self, datalayer: Datalayer, host: str | None = None) -> base_models.BigFileAccessGrant:
        """Return temporary credentials for reading this big file."""
        del host
        return datalayer.generate_bigfile_access_grant(self)

    def get_access_grant(self, datalayer: Datalayer) -> base_models.BigFileAccessGrant:
        """Return temporary credentials for reading the object."""
        return self.grant_read_access(datalayer)

    def fill_info(self, datalayer: Datalayer | None = None) -> None:
        """Mark the object as populated and normalize its stored URI."""
        self.path = self.build_store_path(datalayer)
        # Measured here rather than beside the finish mutation because `fill_info` is the
        # one point both entry paths reach: the `finish*Upload` mutations and the core
        # create mutations, which call this directly. Non-fatal, and it never overwrites a
        # recorded size with null -- see `measured_size`.
        measured = self.measured_size(datalayer)
        if measured is not None:
            self.size_bytes = measured
        self.populated = True
        self.save(update_fields=["path", "populated", "size_bytes"])

    def get_presigned_url(
        self,
        datalayer: Datalayer,
        host: str | None = None,
    ) -> str:
        """Return the canonical S3 path for the object."""
        del host
        return self.build_store_path(datalayer)

    def calculate_size(self, datalayer: Datalayer) -> int:
        """Calculate the size of the big file by querying the datalayer."""
        return datalayer.get_object_size(self.bucket, self.key)


