import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from app.config import settings

MAX_UPLOAD_BYTES = 100 * 1024 * 1024

s3 = boto3.client("s3", endpoint_url=settings.s3_endpoint_url,
                  aws_access_key_id=settings.s3_access_key,
                  aws_secret_access_key=settings.s3_secret_key,
                  region_name=settings.s3_region,
                  config=Config(signature_version="s3v4",
                                connect_timeout=settings.dependency_timeout_seconds,
                                read_timeout=settings.dependency_timeout_seconds,
                                retries={"total_max_attempts": 2, "mode": "standard"}))

def ensure_bucket():
    buckets = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
    if settings.s3_bucket not in buckets:
        s3.create_bucket(Bucket=settings.s3_bucket)

def put_file(key: str, body, content_type: str):
    ensure_bucket()
    s3.upload_fileobj(body, settings.s3_bucket, key, ExtraArgs={"ContentType": content_type})


def file_exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=settings.s3_bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def delete_file(key: str) -> None:
    s3.delete_object(Bucket=settings.s3_bucket, Key=key)


async def read_upload(upload, limit: int = MAX_UPLOAD_BYTES) -> bytes:
    """Read at most limit+1 bytes so callers can reject oversized streams."""
    return await upload.read(limit + 1)

def get_file(key: str):
    return s3.get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read()
