from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from app import storage


def test_existing_bucket_needs_no_global_listing(monkeypatch):
    client = Mock()
    monkeypatch.setattr(storage, "s3", client)
    storage.ensure_bucket()
    client.head_bucket.assert_called_once_with(Bucket=storage.settings.s3_bucket)
    client.list_buckets.assert_not_called()
    client.create_bucket.assert_not_called()


@pytest.mark.parametrize("code,created", [("NoSuchBucket", True), ("403", False), ("500", False)])
def test_only_missing_bucket_is_created(monkeypatch, code, created):
    client = Mock()
    client.head_bucket.side_effect = ClientError({"Error": {"Code": code}}, "HeadBucket")
    monkeypatch.setattr(storage, "s3", client)
    if created:
        storage.ensure_bucket()
        client.create_bucket.assert_called_once_with(Bucket=storage.settings.s3_bucket)
    else:
        with pytest.raises(ClientError):
            storage.ensure_bucket()
        client.create_bucket.assert_not_called()
