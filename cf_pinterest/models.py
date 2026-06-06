from dataclasses import dataclass


@dataclass(slots=True)
class QueueItem:
    slug: str
    title: str
    niche: str
    status: str
    pin_jpg: str
    cf_url: str
    affiliate_url: str
    image_url: str
    source_file: str


@dataclass(slots=True)
class QueueSyncResult:
    parsed_items: int
    upserted_items: int
    skipped_items: int
    generated_items: int
    uploaded_items: int
    rejected_items: int
