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
        aws_access_key_id=s.s3_access_key_id,
        aws_secret_access_key=s.s3_secret_access_key,
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


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(ClientError),
)
def fetch_zip_bytes(zip_key: str) -> bytes:
    """
    Fetch a ZIP file from object storage and return its binary content.
    
    Args:
        zip_key: Object key or pre-signed URL of the ZIP file in storage
        
    Returns:
        bytes: Binary content of the ZIP file
        
    Raises:
        ClientError: If the S3/object storage operation fails
        ValueError: If the file is larger than MAX_UNZIPPED_BYTES
    """
    settings = get_settings()
    client = _make_client()

    if zip_key.startswith("http"):
        key = _extract_key_from_url(zip_key)
    else:
        key = zip_key

    bucket = _bucket_for_zip_key(key)
    log.info("fetch_zip", key=key, bucket=bucket)
    
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        zip_bytes = resp["Body"].read()
        
        # Validate file size
        if len(zip_bytes) > 50 * 1024 * 1024:  # 50 MB limit
            log.error("zip_file_too_large", key=key, bucket=bucket, size_mb=len(zip_bytes) / (1024 * 1024))
            raise ValueError(f"ZIP file exceeds 50 MB limit: {len(zip_bytes)} bytes")
        
        log.info("zip_fetched_success", key=key, bucket=bucket, size_bytes=len(zip_bytes))
        return zip_bytes
        
    except ClientError as exc:
        log.error("zip_fetch_failed", key=key, bucket=bucket, error=str(exc), error_code=exc.response.get("Error", {}).get("Code"))
        raise
    except Exception as exc:
        log.error("zip_fetch_error", key=key, bucket=bucket, error=str(exc), error_type=type(exc).__name__)
        raise


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(ClientError),
)
def upload_extracted_files(
    submission_id: str,
    extracted_contents: dict[str, str],
) -> dict[str, str]:
    """
    Upload extracted files to object storage and return presigned URLs.
    
    Each file is stored with key: submissions/{submission_id}/{filename}
    and returns a presigned URL valid for storage_presigned_url_expiry seconds.
    
    Args:
        submission_id: Unique identifier for the submission
        extracted_contents: Dict mapping filename → file content (text)
        
    Returns:
        dict: Mapping of filename → presigned URL for each uploaded file
        
    Raises:
        ClientError: If S3/object storage operations fail
        ValueError: If submission_id is empty or contains invalid characters
    """
    if not submission_id or not submission_id.strip():
        raise ValueError("submission_id cannot be empty")
    
    if any(char in submission_id for char in ['/', '\\', '\x00']):
        raise ValueError(f"Invalid characters in submission_id: {submission_id}")
    
    settings = get_settings()
    client = _make_client()
    artifact_urls: dict[str, str] = {}
    failed_uploads: list[str] = []
    
    log.info("upload_extracted_files_start", submission_id=submission_id, file_count=len(extracted_contents))
    
    for filename, content in extracted_contents.items():
        try:
            # Construct object key with submission hierarchy
            object_key = f"submissions/{submission_id}/extracted/{filename}"
            
            # Determine content type
            content_type = _get_content_type(filename)
            
            # Skip binary file markers
            if content.startswith("[binary:"):
                log.info("skipping_binary_file", filename=filename, submission_id=submission_id)
                continue
            
            # Upload file
            log.info("uploading_file", filename=filename, key=object_key)
            client.put_object(
                Bucket=settings.storage_bucket,
                Key=object_key,
                Body=content.encode("utf-8"),
                ContentType=content_type,
            )
            
            # Generate presigned URL
            presigned_url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.storage_bucket, "Key": object_key},
                ExpiresIn=settings.storage_presigned_url_expiry,
            )
            
            artifact_urls[filename] = presigned_url
            log.info("file_uploaded_success", filename=filename, submission_id=submission_id)
            
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "Unknown")
            log.error(
                "file_upload_failed",
                filename=filename,
                submission_id=submission_id,
                error_code=error_code,
                error=str(exc),
            )
            failed_uploads.append(filename)
        except Exception as exc:
            log.error(
                "file_upload_error",
                filename=filename,
                submission_id=submission_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            failed_uploads.append(filename)
    
    if failed_uploads:
        log.warning(
            "partial_upload_failure",
            submission_id=submission_id,
            failed_count=len(failed_uploads),
            failed_files=failed_uploads,
            successful_count=len(artifact_urls),
        )
    
    log.info(
        "upload_extracted_files_complete",
        submission_id=submission_id,
        uploaded_count=len(artifact_urls),
        total_count=len(extracted_contents),
    )
    
    return artifact_urls


def _get_content_type(filename: str) -> str:
    """
    Determine MIME type based on file extension.
    
    Args:
        filename: The name of the file
        
    Returns:
        str: Appropriate Content-Type for the file
    """
    extension_to_type = {
        ".py": "text/x-python",
        ".txt": "text/plain",
        ".env": "text/plain",
        ".md": "text/markdown",
        ".json": "application/json",
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
        ".csv": "text/csv",
        ".html": "text/html",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".toml": "application/toml",
        ".cfg": "text/plain",
        ".ini": "text/plain",
        ".ipynb": "application/json",
    }
    
    # Extract extension (case-insensitive)
    ext = filename.lower()[filename.rfind("."):] if "." in filename else ""
    
    return extension_to_type.get(ext, "application/octet-stream")


def _bucket_for_zip_key(key: str) -> str:
    settings = get_settings()
    if key.startswith("assessments/"):
        return settings.assessment_storage_bucket
    return settings.storage_bucket


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
