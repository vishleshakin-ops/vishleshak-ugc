import os
from pathlib import Path

import cloudinary
import cloudinary.api
import cloudinary.uploader
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True,
)

JOBS = [
    (
        Path(os.getenv("SAMPLE_IMAGES_DIR", r"D:\Sample Images")),
        "image",
        os.getenv("CLOUDINARY_SAMPLE_IMAGE_PREFIX", "vishleshak-samples/images"),
        {".jpg", ".jpeg", ".png", ".webp"},
    ),
    (
        Path(os.getenv("SAMPLE_VIDEOS_DIR", r"D:\Sample Videos")),
        "video",
        os.getenv("CLOUDINARY_SAMPLE_VIDEO_PREFIX", "vishleshak-samples/videos"),
        {".mp4", ".mov", ".webm", ".m4v"},
    ),
]


def cloudinary_resource_exists(public_id: str, resource_type: str) -> bool:
    try:
        cloudinary.api.resource(public_id, resource_type=resource_type)
        return True
    except Exception as exc:
        message = str(exc).lower()
        if "not found" in message or "404" in message:
            return False
        raise


def main() -> None:
    if not all(
        [
            os.getenv("CLOUDINARY_CLOUD_NAME"),
            os.getenv("CLOUDINARY_API_KEY"),
            os.getenv("CLOUDINARY_API_SECRET"),
        ]
    ):
        raise SystemExit("Missing Cloudinary settings in .env")

    uploaded = 0
    skipped = 0
    for folder, resource_type, prefix, extensions in JOBS:
        print(f"\nSyncing {folder} -> {prefix}")
        if not folder.exists():
            print(f"  Skipped: folder not found")
            continue

        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            public_id = f"{prefix}/{path.stem}"
            if cloudinary_resource_exists(public_id, resource_type):
                skipped += 1
                print(f"  Skipped existing {path.name}")
                continue
            result = cloudinary.uploader.upload(
                str(path),
                resource_type=resource_type,
                public_id=public_id,
                overwrite=False,
            )
            uploaded += 1
            print(f"  Uploaded {path.name}")
            print(f"    {result.get('secure_url')}")

    print(f"\nDone. Uploaded {uploaded} new file(s); skipped {skipped} existing file(s).")


if __name__ == "__main__":
    main()
