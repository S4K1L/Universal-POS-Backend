import uuid

from fastapi import APIRouter, Depends, File, UploadFile

from app.api.deps import Ctx, tenant_ctx
from app.core.config import settings
from app.core.errors import DomainError, ValidationFailed
from app.core.permissions import Permission

router = APIRouter(prefix="/uploads", tags=["Catalogue"])


class StorageUnavailable(DomainError):
    status_code = 503

# Only raster images, recognised by their first bytes — never by the filename or the declared content type,
# which a client controls. SVG is refused on purpose: it can carry script.
_SIGNATURES = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def sniff(head: bytes) -> str | None:
    for magic, ext in _SIGNATURES:
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


@router.post("/images", status_code=201, summary="Upload a product picture (JPEG, PNG, WebP or GIF); returns the path to store on the product")
async def upload_image(file: UploadFile = File(...), ctx: Ctx = Depends(tenant_ctx(permission=Permission.manage_products, write=True))) -> dict:
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailed(f"That picture is larger than {settings.max_upload_mb} MB", code="image_too_large")
    ext = sniff(data[:16])
    if ext is None:
        raise ValidationFailed("Choose a JPEG, PNG, WebP or GIF picture", code="unsupported_image")
    folder = settings.upload_root / str(ctx.tenant.id)
    name = f"{uuid.uuid4().hex}.{ext}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
    except OSError as exc:
        raise StorageUnavailable("Picture storage is not available on the server right now", code="storage_unavailable") from exc
    return {"url": f"/uploads/{ctx.tenant.id}/{name}", "bytes": len(data)}
