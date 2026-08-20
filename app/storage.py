import boto3
from botocore.client import Config
from app.config import settings

s3 = boto3.client("s3", endpoint_url=settings.s3_endpoint_url,
                  aws_access_key_id=settings.s3_access_key,
                  aws_secret_access_key=settings.s3_secret_key,
                  region_name=settings.s3_region,
                  config=Config(signature_version="s3v4"))

def ensure_bucket():
    buckets = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
    if settings.s3_bucket not in buckets:
        s3.create_bucket(Bucket=settings.s3_bucket)

def put_file(key: str, body, content_type: str):
    ensure_bucket()
    s3.upload_fileobj(body, settings.s3_bucket, key, ExtraArgs={"ContentType": content_type})

def get_file(key: str):
    return s3.get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read()
