import strawberry
from strawberry.scalars import JSON
from datalayer import models
from kante.types import Info
import kante
from typing import cast
from datalayer import base_models
from datalayer.scalars import ByteCount
from datalayer.datalayer import get_current_datalayer


@kante.pydantic_type(base_models.BigFileAccessGrant, description="Temporary S3 credentials for reading a big file.")
class BigFileAccessGrant:
    """Temporary S3 credentials for a big file."""

    status: str
    access_key: str
    secret_key: str
    session_token: str
    region: str

    bucket: str
    key: str
    path: str
    expires_in: int
    store: str | None


@kante.pydantic_type(base_models.BigFileUploadGrant, description="Temporary S3 credentials for uploading a big file.")
class BigFileUploadGrant:
    """Temporary S3 credentials for a big file upload."""

    region: str
    status: str
    access_key: str
    secret_key: str
    session_token: str
    bucket: str
    key: str
    path: str
    expires_in: int
    max_bytes: ByteCount
    original_file_name: str | None
    upload_file_name: str
    upload_content_type: str | None
    upload_form_field: str
    store: str


@kante.django_type(
    models.BigFileStore,
    description="A BigFileStore represents a large object stored behind the S3 datalayer.",
)
class BigFileStore:
    """A large object stored behind the S3 datalayer."""

    id: strawberry.auto
    path: str
    bucket: str
    key: str
    size_bytes: ByteCount | None = strawberry.field(description="How many bytes this store actually holds, measured when its upload was finished. Null while unfinished, or for stores written before this was recorded")
    original_file_name: str | None
    content_type: str | None

    @strawberry.field(description="Get temporary S3 read credentials for the object.")
    def access_grant(self, info: Info, host: str | None = None) -> BigFileAccessGrant:
        """Return a signed read grant for the big file."""
        del info, host
        datalayer = get_current_datalayer()
        grant = cast(models.BigFileStore, self).get_access_grant(datalayer=datalayer)
        return BigFileAccessGrant.from_pydantic(grant)

    @strawberry.field()
    def presigned_url(self, info: Info) -> str:
        """Compatibility field returning the canonical S3 object path."""
        datalayer = get_current_datalayer()
        return cast(models.BigFileStore, self).get_presigned_url(datalayer=datalayer)


