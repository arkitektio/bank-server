import json
import logging
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Optional, TypeVar, cast

import boto3
from botocore.config import Config
from django.conf import settings
from pydantic import AliasChoices, BaseModel, ByteSize, ConfigDict, Field

from datalayer import base_models
from datalayer import quota as quota_module

if TYPE_CHECKING:
    from authentikate.models import User

    from datalayer import models

logger = logging.getLogger(__name__)


AccessGrant = base_models.AccessGrant
StoreModel = TypeVar("StoreModel", bound="models.DatalayerStore")

#: STS refuses a shorter session, and a refused `AssumeRole` used to fall through to the
#: service's permanent key -- so a caller asking for five minutes silently got forever.
#: Clamping here keeps `expires_in` a preference rather than a way out.
MIN_SESSION_DURATION_SECONDS = 900

#: The other end of the same clamp. `expires_in` reaches this from a GraphQL input, so a
#: caller must not be able to pick a duration STS will reject -- refusing at both ends is a
#: bound, refusing at one is a way to turn a grant into an error.
MAX_SESSION_DURATION_SECONDS = 43200


# Context variable for the datalayer instance
datalayer: ContextVar["Datalayer"] = ContextVar("datalayer")


class BucketConfig(BaseModel):
    """Resolved bucket configuration for one datalayer store type."""

    bucket: str = Field(..., validation_alias=AliasChoices("PATH", "path"))
    subpath: str | None = Field(None, validation_alias=AliasChoices("SUBPATH", "subpath"))
    default_max_bytes: ByteSize = Field(
        ByteSize(100 * 1024 * 1024),
        validation_alias=AliasChoices("DEFAULT_MAX_BYTES", "default_max_bytes"),
    )

    model_config = ConfigDict(populate_by_name=True)


class DatalayerConfig(BaseModel):
    """Runtime configuration loaded from ``settings.DATALAYER``."""

    role_arn: str | None = Field(None, validation_alias=AliasChoices("ROLE_ARN", "role_arn"))
    external_id: str | None = Field(None, validation_alias=AliasChoices("EXTERNAL_ID", "external_id"))
    session_duration_seconds: int = Field(
        3600,
        validation_alias=AliasChoices("SESSION_DURATION_SECONDS", "session_duration_seconds"),
    )
    allow_unscoped_fallback: bool = Field(
        False,
        validation_alias=AliasChoices("ALLOW_UNSCOPED_FALLBACK", "allow_unscoped_fallback"),
        description="Hand out this service's own permanent credentials when no scoped session can be issued. Development only -- it makes every grant unlimited in scope and lifetime.",
    )
    access_key: str | None = Field(
        None,
        validation_alias=AliasChoices("AWS_ACCESS_KEY_ID", "aws_access_key_id", "access_key"),
    )
    secret_key: str | None = Field(
        None,
        validation_alias=AliasChoices("AWS_SECRET_ACCESS_KEY", "aws_secret_access_key", "secret_key"),
    )
    session_token: str | None = Field(
        None,
        validation_alias=AliasChoices("AWS_SESSION_TOKEN", "aws_session_token", "session_token"),
    )
    host: str | None = Field(
        None,
        validation_alias=AliasChoices("AWS_S3_ENDPOINT_URL", "aws_s3_endpoint_url", "host"),
    )
    region: str = Field(
        "us-east-1",
        validation_alias=AliasChoices("AWS_S3_REGION_NAME", "aws_s3_region_name", "region"),
    )
    port: int | None = Field(None, validation_alias=AliasChoices("AWS_S3_PORT", "aws_s3_port", "port"))
    protocol: str = Field(
        "https",
        validation_alias=AliasChoices("AWS_S3_URL_PROTOCOL", "aws_s3_url_protocol", "protocol"),
    )

    bigfile: Optional[BucketConfig] = None

    upload_roles: list[str] = Field(
        default_factory=lambda: ["admin", "editor", "bot"],
        validation_alias=AliasChoices("UPLOAD_ROLES", "upload_roles"),
        description="Organization roles allowed to request upload grants; any one of them is enough.",
    )
    quotas: quota_module.QuotaConfig = Field(
        default_factory=quota_module.QuotaConfig,
        validation_alias=AliasChoices("QUOTAS", "quotas"),
    )

    model_config = ConfigDict(populate_by_name=True)

    @property
    def endpoint_url(self) -> Optional[str]:
        """Construct the full endpoint URL if host and port are provided."""
        if not self.host:
            return None
        if self.port is None:
            return f"{self.protocol}://{self.host}"
        return f"{self.protocol}://{self.host}:{self.port}"


class Datalayer:
    """Generate temporary S3 grants and manage datalayer-backed stores."""

    def __init__(self) -> None:
        """Initialize storage clients.

        The datalayer reads all connection and bucket configuration from
        ``settings.DATALAYER``.
        """
        self.config = DatalayerConfig(**getattr(settings, "DATALAYER", {}))

        client_kwargs = {
            "aws_access_key_id": self.config.access_key,
            "aws_secret_access_key": self.config.secret_key,
            "endpoint_url": self.config.endpoint_url,
            "region_name": self.config.region,
            "config": Config(signature_version="s3v4"),
        }
        if self.config.session_token:
            client_kwargs["aws_session_token"] = self.config.session_token

        self._s3 = boto3.client("s3", **client_kwargs)
        self._sts = boto3.client("sts", **client_kwargs)

    def get_bucket_config(self, bucket_key: str) -> BucketConfig:
        """Return bucket configuration for a known datalayer store.

        Args:
            bucket_key: Logical store type (``bigfile``).

        Returns:
            The resolved bucket configuration.

        Raises:
            ValueError: If the bucket key is not configured.
        """
        conf = getattr(self.config, bucket_key, None)
        if conf is not None:
            return conf

        else:
            raise ValueError(f"Service/Bucket '{bucket_key}' not configured in datalayer.")

    def build_object_key(self, bucket_key: str, object_path: str) -> str:
        """Build the concrete S3 key for a logical object path.

        Args:
            bucket_key: Logical datalayer store type.
            object_path: Store-relative object key or prefix.

        Returns:
            The S3 object key including any configured bucket subpath.
        """
        conf = self.get_bucket_config(bucket_key)
        if conf.subpath:
            return f"{conf.subpath.rstrip('/')}/{object_path.lstrip('/')}"
        return object_path.lstrip("/")

    def build_store_path(self, bucket_key: str, object_path: str) -> str:
        """Build the canonical S3 URI stored in the database.

        Args:
            bucket_key: Logical datalayer store type.
            object_path: Store-relative object key or prefix.

        Returns:
            A canonical ``s3://`` URI.
        """
        conf = self.get_bucket_config(bucket_key)
        return f"s3://{conf.bucket}/{self.build_object_key(bucket_key, object_path)}"

    def _parse_s3_path(self, path: str) -> tuple[str, str]:
        """Parse a canonical S3 URI into bucket and key parts.

        Args:
            path: Canonical ``s3://`` URI.

        Returns:
            The bucket name and object key prefix.

        Raises:
            ValueError: If the path is not a valid ``s3://`` URI.
        """
        if not path.startswith("s3://"):
            raise ValueError(f"Invalid S3 path: {path}")

        bucket_name, key = path.removeprefix("s3://").split("/", 1)
        return bucket_name, key

    def _new_key(self) -> str:
        """Generate a new opaque storage key.

        Returns:
            A random hex key suitable for store creation.
        """
        return uuid.uuid4().hex

    def _session_duration(self, expires_in: int | None = None) -> int:
        """Resolve a credential lifetime.

        Args:
            expires_in: Optional explicit duration override in seconds.

        Returns:
            The requested duration or the configured default, clamped to what STS accepts.
        """
        requested = expires_in or self.config.session_duration_seconds
        return min(max(requested, MIN_SESSION_DURATION_SECONDS), MAX_SESSION_DURATION_SECONDS)

    def resolve_quota(self, organization_id: int, user: Optional["User"]) -> quota_module.Quota:
        """The configured quota for ``user`` acting in the organization. See :mod:`datalayer.quota`."""
        from authentikate.models import Organization

        slug = Organization.objects.values_list("slug", flat=True).get(id=organization_id)
        return quota_module.resolve_quota(self.config.quotas, slug, user.sub if user is not None else None)

    def _upload_budget(self, bucket_key: str, quota: quota_module.Quota, declared: Optional[int]) -> int:
        """The byte budget a grant advertises: the declared size, else the per-upload quota, else the bucket default."""
        if declared:
            return declared
        if quota.max_upload_bytes is not None:
            return quota.max_upload_bytes
        return int(self.get_bucket_config(bucket_key).default_max_bytes)

    def _admit_upload(self, bucket_key: str, organization_id: int, user: Optional["User"], declared: Optional[int]) -> int:
        """Check a new upload against the quota, and return the byte budget its grant advertises.

        Raises :class:`~datalayer.quota.QuotaExceeded` when the organization or the user is
        over quota, or the declared size is over the per-upload limit.
        """
        quota = self.resolve_quota(organization_id, user)
        quota_module.check_upload(quota, organization_id, user, declared)
        return self._upload_budget(bucket_key, quota, declared)


    def _object_resources(self, bucket_key: str, object_path: str) -> tuple[str, list[str], bool]:
        """Resolve S3 resources covered by a grant.

        Args:
            bucket_key: Logical datalayer store type.
            object_path: Store-relative object key or prefix.

        Returns:
            A tuple containing the full object key, the covered resource paths,
            and whether bucket listing permission is also required.
        """
        full_key = self.build_object_key(bucket_key, object_path)
        return full_key, [full_key], False

    def _build_policy(self, bucket_name: str, bucket_key: str, object_path: str, action: str) -> dict[str, object]:
        """Build an inline session policy for an assumed role.

        Args:
            bucket_name: Physical S3 bucket name.
            bucket_key: Logical datalayer store type.
            object_path: Store-relative object key or prefix.
            action: Requested action such as ``read`` or ``upload``.

        Returns:
            An IAM policy document scoped to the requested object resources.
        """
        _, resources, _ = self._object_resources(bucket_key, object_path)
        s3_resources = [f"arn:aws:s3:::{bucket_name}/{resource}" for resource in resources]
        action_map = {
            "read": ["s3:GetObject"],
            "upload": ["s3:PutObject", "s3:AbortMultipartUpload"],
            "delete": ["s3:DeleteObject"],
        }
        statements: list[dict[str, object]] = [
            {
                "Effect": "Allow",
                "Action": action_map[action],
                "Resource": s3_resources,
            }
        ]

        return {"Version": "2012-10-17", "Statement": statements}


    def _assume_role(self, action: str, duration: int, policy: dict[str, object] | None) -> tuple[str, str, str]:
        """Mint temporary credentials by assuming the configured role.

        The only way this codebase obtains scoped credentials. There is deliberately no
        ``get_session_token`` path any more: MinIO does not route that STS action at all
        (``InvalidParameterValue: Unsupported action GetSessionToken``), so it could only ever
        fail, and it failed *into* the unscoped fallback -- which is why a deployment could run
        for a long time handing out permanent keys with nothing in the logs.

        Args:
            action: Requested action, used to label the STS session.
            duration: Credential lifetime in seconds.
            policy: Inline session policy, or ``None`` for a session bounded only by the
                service account's own policy.

        Returns:
            A tuple of access key, secret key, and session token.

        Raises:
            RuntimeError: If no role is configured, or if STS refuses the request.
        """
        if not self.config.role_arn:
            raise RuntimeError("`DATALAYER.role_arn` is unset, so there is no role to assume and no scoped credentials can be issued. Against MinIO the value is ignored -- any ARN-shaped string will do -- and the session is scoped by the inline policy alone.")

        assume_role_kwargs: dict[str, object] = {
            "RoleArn": self.config.role_arn,
            "RoleSessionName": f"bank-{action}-{uuid.uuid4().hex[:8]}",
            "DurationSeconds": duration,
        }
        if policy is not None:
            assume_role_kwargs["Policy"] = json.dumps(policy)
        if self.config.external_id:
            assume_role_kwargs["ExternalId"] = self.config.external_id

        try:
            credentials = self._sts.assume_role(**assume_role_kwargs)["Credentials"]
        except Exception as exc:
            raise RuntimeError(f"STS refused to issue credentials for a `{action}` session ({exc}).") from exc

        return (
            credentials["AccessKeyId"],
            credentials["SecretAccessKey"],
            credentials["SessionToken"],
        )

    def _unscoped_fallback(self, what: str, cause: Exception) -> tuple[str, str, str]:
        """Hand back this service's own permanent credentials, if that is explicitly allowed.

        This used to be the unconditional behaviour on *any* STS failure, and it is why the
        scoping machinery above was inert: a grant that could not be scoped was indistinguishable
        from one that was, because both returned usable credentials and neither logged. The
        credentials handed out here are the service account's -- cluster-wide ``readwrite``,
        no expiry -- so failing is almost always the better answer.

        Args:
            what: Description of the grant being issued, for the operator reading the log.
            cause: The failure that led here.

        Returns:
            The configured long-lived credentials.

        Raises:
            RuntimeError: Unless ``DATALAYER.allow_unscoped_fallback`` is set.
        """
        if not self.config.allow_unscoped_fallback:
            raise RuntimeError(f"Could not issue {what}. Refusing to fall back to this service's own permanent credentials, which are unscoped and never expire; set `DATALAYER.allow_unscoped_fallback` to accept that in development.") from cause

        logger.warning("Issuing %s with this service's own permanent credentials because no session could be minted (%s). The client receives an unscoped, non-expiring key.", what, cause)
        return (
            self.config.access_key or "",
            self.config.secret_key or "",
            self.config.session_token or "",
        )

    def _issue_temporary_credentials(self, bucket_key: str, object_path: str, action: str, expires_in: int) -> tuple[str, str, str]:
        """Issue temporary credentials scoped to one store's objects.

        Args:
            bucket_key: Logical datalayer store type.
            object_path: Store-relative object key or prefix.
            action: Requested action such as ``read`` or ``upload``.
            expires_in: Requested credential lifetime in seconds.

        Returns:
            A tuple of access key, secret key, and session token.

        Raises:
            RuntimeError: If no scoped session could be issued and the unscoped fallback is off.
        """
        conf = self.get_bucket_config(bucket_key)
        duration = self._session_duration(expires_in)
        policy = self._build_policy(conf.bucket, bucket_key, object_path, action)

        try:
            return self._assume_role(action, duration, policy)
        except Exception as exc:
            return self._unscoped_fallback(f"a `{action}` grant on {bucket_key} store {object_path}", exc)


    def generate_bigfile_upload_grant(self, organization_id: int, input: base_models.RequestBigFileUploadInput, user: Optional["User"] = None) -> base_models.BigFileUploadGrant:
        """Create a big file store and upload grant."""
        from datalayer import models

        conf = self.get_bucket_config("bigfile")

        budget = self._admit_upload("bigfile", organization_id, user, input.file_size)
        key = self._new_key()
        store = models.BigFileStore.objects.create(
            organization_id=organization_id,
            creator=user,
            path=self.build_store_path("bigfile", key),
            key=key,
            bucket="bigfile",
            original_file_name=input.original_file_name,
            content_type=input.content_type,
        )

        ttl = self._session_duration()

        access_key, secret_key, session_token = self._issue_temporary_credentials("bigfile", store.key, "upload", ttl)
        full_key = self.build_object_key("bigfile", store.key)

        return base_models.BigFileUploadGrant(
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token,
            bucket=conf.bucket,
            region=self.config.region,
            key=full_key,
            path=self.build_store_path("bigfile", store.key),
            expires_in=ttl,
            datalayer="bigfile",
            max_bytes=budget,
            original_file_name=store.original_file_name,
            upload_file_name=store.get_upload_file_name(),
            upload_content_type=store.content_type,
            upload_form_field="file",
            store=str(store.pk),
        )


    def _finish_store_upload(self, model_class: type[StoreModel], organization_id: int, store_id: str, valid: bool) -> StoreModel:
        """Finalize a created store after upload completion.

        Args:
            model_class: Store model type to load.
            store_id: Primary key of the store row.
            valid: Whether the upload succeeded and should be marked populated.

        Returns:
            The updated store instance.
        """
        store = model_class.objects.get(id=store_id, organization_id=organization_id)
        if valid:
            store.fill_info(self)
        else:
            store.populated = False
            store.save(update_fields=["populated"])
        return cast(StoreModel, store)


    def finish_bigfile_upload(self, organization_id: int, input: base_models.FinishBigFileUploadInput) -> "models.BigFileStore":
        """Mark a big file upload as complete.

        Args:
            input: Completion payload for the big file store.

        Returns:
            The finalized big file store.
        """
        from datalayer import models

        return self._finish_store_upload(models.BigFileStore, organization_id, input.store_id, input.valid)


    def get_object_size(self, bucket_name: str, object_key: str) -> int:
        """Get the size of an object in bytes.

        Args:
            bucket_name: The name of the S3 bucket.
            object_key: The key of the S3 object.
        Returns:
            The size of the object in bytes.
        """
        bucket_config = self.get_bucket_config(bucket_name)
        if bucket_config is None:
            raise ValueError(f"Bucket '{bucket_name}' is not configured in datalayer.")

        try:
            response = self._s3.head_object(Bucket=bucket_config.bucket, Key=object_key)
            return response["ContentLength"]
        except Exception as exc:
            raise FileNotFoundError(f"Could not retrieve object size for s3://{bucket_name}/{object_key}.") from exc

    def generate_file_read_url(
        self,
        bucket_key: str,
        object_path: str,
        *,
        store_id: str | None = None,
        expires_in: int | None = None,
    ) -> AccessGrant:
        """Build a generic read access grant.

        Args:
            bucket_key: Logical datalayer store type.
            object_path: Store-relative object key or prefix.
            store_id: Optional backing store identifier.
            expires_in: Optional credential lifetime override in seconds.

        Returns:
            Temporary credentials scoped to reading the requested object.
        """
        conf = self.get_bucket_config(bucket_key)
        ttl = self._session_duration(expires_in)
        access_key, secret_key, session_token = self._issue_temporary_credentials(bucket_key, object_path, "read", ttl)
        full_key = self.build_object_key(bucket_key, object_path)
        return base_models.AccessGrant(
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token,
            bucket=conf.bucket,
            key=full_key,
            path=self.build_store_path(bucket_key, object_path),
            action="read",
            expires_in=ttl,
            datalayer=bucket_key,
            endpoint=self.config.endpoint_url or "",
            store=str(store_id) if store_id is not None else None,
        )


    def generate_bigfile_access_grant(
        self,
        store: "models.BigFileStore",
        *,
        expires_in: int | None = None,
    ) -> base_models.BigFileAccessGrant:
        """Build a big file read access grant.

        Args:
            store: Big file store to grant access to.
            expires_in: Optional credential lifetime override in seconds.

        Returns:
            Temporary credentials scoped to reading the big file object.
        """
        object_path = store.key
        store_id = str(store.pk) if store.pk is not None else None
        conf = self.get_bucket_config("bigfile")
        ttl = self._session_duration(expires_in)
        access_key, secret_key, session_token = self._issue_temporary_credentials("bigfile", object_path, "read", ttl)
        full_key = self.build_object_key("bigfile", object_path)
        return base_models.BigFileAccessGrant(
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token,
            bucket=conf.bucket,
            region=self.config.region,
            key=full_key,
            path=self.build_store_path("bigfile", object_path),
            action="read",
            expires_in=ttl,
            datalayer="bigfile",
            endpoint=self.config.endpoint_url or "",
            store=str(store_id) if store_id is not None else None,
        )


    def put_file(
        self,
        bucket_key: str,
        object_path: str,
        payload: bytes,
        content_type: str | None = None,
    ) -> None:
        """Upload a single object with service credentials.

        Args:
            bucket_key: Logical datalayer store type.
            object_path: Store-relative object key.
            payload: File bytes to upload.
            content_type: Optional MIME type for the object.
        """
        conf = self.get_bucket_config(bucket_key)
        self._s3.put_object(
            Bucket=conf.bucket,
            Key=self.build_object_key(bucket_key, object_path),
            Body=payload,
            ContentType=content_type or "application/octet-stream",
        )

    def read_object(self, bucket_key: str, object_path: str) -> bytes:
        """Read a whole single object with service credentials (a server-side import of an uploaded file).

        Args:
            bucket_key: Logical datalayer store type.
            object_path: Store-relative object key.
        """
        conf = self.get_bucket_config(bucket_key)
        return self._s3.get_object(Bucket=conf.bucket, Key=self.build_object_key(bucket_key, object_path))["Body"].read()

    def delete_object(self, bucket_key: str, object_path: str) -> None:
        """Delete a single object with service credentials.

        Args:
            bucket_key: Logical datalayer store type.
            object_path: Store-relative object key.
        """
        conf = self.get_bucket_config(bucket_key)
        self._s3.delete_object(
            Bucket=conf.bucket,
            Key=self.build_object_key(bucket_key, object_path),
        )


GLOBAL_DL = None


def get_current_datalayer() -> Datalayer:
    """Return the request-scoped datalayer instance.

    Returns:
        The datalayer instance currently bound to the active request context.
    """
    global GLOBAL_DL
    if GLOBAL_DL is not None:
        return GLOBAL_DL

    else:
        GLOBAL_DL = Datalayer()
        return GLOBAL_DL
