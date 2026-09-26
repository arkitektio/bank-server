"""The vendored datalayer against the stack's real RustFS: a grant is an STS session scoped to one key."""

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from datalayer import models as dl_models

from .conftest import REQUEST_UPLOAD


async def test_upload_round_trip(upload, datalayer, authenticated_context):
    store_id = await upload(b"Buchungstag;Betrag\n", name="export.csv")

    store = await dl_models.BigFileStore.objects.aget(id=store_id)
    assert store.populated and store.organization_id == authenticated_context.request.organization.id
    assert datalayer.read_object(store.bucket, store.key) == b"Buchungstag;Betrag\n"


async def test_a_grant_writes_only_its_own_key(aexecute, datalayer, backend_stack):
    grant = (await aexecute(REQUEST_UPLOAD, {"name": "a.xlsx", "size": 3})).data["requestBigfileUpload"]
    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://localhost:{backend_stack.rustfs_port}",
        aws_access_key_id=grant["accessKey"],
        aws_secret_access_key=grant["secretKey"],
        aws_session_token=grant["sessionToken"],
        region_name=grant["region"],
        config=Config(signature_version="s3v4"),
    )

    with pytest.raises(ClientError):
        s3.put_object(Bucket=grant["bucket"], Key="someone-else", Body=b"x")
    s3.put_object(Bucket=grant["bucket"], Key=grant["key"], Body=b"abc")
