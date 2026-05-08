from __future__ import annotations
import boto3
from botocore.exceptions import ClientError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config.settings import get_settings
from utils.logger import get_logger

log = get_logger(__name__)


def _make_client():
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.storage_endpoint_url,
        aws_access_key_id=s.storage_access_key,
        aws_secret_access_key=s.storage_secret_key,
        region_name="us-east-1",
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(ClientError),
)
def fetch_file_content(url_or_key: str) -> str:
    """
    Fetch a file from object storage and return its text content.
    Accepts either a full pre-signed URL or a bare object key.
    """
    settings = get_settings()
    client   = _make_client()

    if url_or_key.startswith("http"):
        key = _extract_key_from_url(url_or_key)
    else:
        key = url_or_key

    log.info("fetch_file", key=key)
    resp = client.get_object(Bucket=settings.storage_bucket, Key=key)
    return resp["Body"].read().decode("utf-8", errors="replace")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(ClientError),
)
def upload_json(key: str, content: str) -> str:
    """
    Upload a JSON string to object storage and return a pre-signed URL.
    """
    settings = get_settings()
    client   = _make_client()

    log.info("upload_json", key=key)
    client.put_object(
        Bucket=settings.storage_bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="application/json",
    )

    presigned_url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.storage_bucket, "Key": key},
        ExpiresIn=settings.storage_presigned_url_expiry,
    )
    return presigned_url


def fetch_all_artifacts(artifact_urls: dict[str, str]) -> dict[str, str]:
    """
    Fetch every artifact and return dict[role → text content].
    Files that fail to fetch are stored as empty strings with a log warning.
    """
    contents: dict[str, str] = {}
    for role, url in artifact_urls.items():
        try:
            contents[role] = fetch_file_content(url)
        except Exception as exc:
            log.warning("artifact_fetch_failed", role=role, error=str(exc))
            contents[role] = ""
    return contents


def _extract_key_from_url(url: str) -> str:
    """
    Strip pre-signed query params and endpoint prefix, return the object key.
    Works for both S3 and MinIO pre-signed URLs.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path   = parsed.path.lstrip("/")
    settings = get_settings()
    bucket   = settings.storage_bucket
    if path.startswith(bucket + "/"):
        path = path[len(bucket) + 1:]
    return path
