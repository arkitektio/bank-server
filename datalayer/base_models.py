from typing import Literal, Optional, cast

from pydantic import BaseModel, JsonValue


class RequestBigFileUploadInput(BaseModel):
    """Request temporary S3 upload credentials for a big file."""

    original_file_name: str
    file_size: Optional[int] = None
    content_type: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None


class FinishBigFileUploadInput(BaseModel):
    """Mark a BigFileStore as populated after a successful upload."""

    store_id: str
    valid: bool = True


class RequestBigFileAccessInput(BaseModel):
    """Request temporary S3 access credentials for a media object."""

    store_id: str


class AccessGrant(BaseModel):
    """Temporary S3 credentials scoped to a datalayer action."""

    status: str = "granted"
    access_key: str
    secret_key: str
    session_token: str
    region: str
    bucket: str
    key: str
    path: str
    expires_in: int
    store: str | None = None


class BigFileAccessGrant(AccessGrant):
    """Temporary S3 credentials for an existing big file."""


class BaseUploadGrant(AccessGrant):
    """Temporary S3 credentials for uploads bound to a specific store."""

    region: str
    max_bytes: int
    original_file_name: str | None = None
    upload_file_name: str
    upload_content_type: str | None = None
    upload_form_field: str = "file"


class BigFileUploadGrant(BaseUploadGrant):
    """Temporary S3 credentials for a big file upload."""


