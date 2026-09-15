from datetime import datetime, timezone
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Dialect-portable JSON type (native JSONB on PostgreSQL, JSON on SQLite)
PortableJSON = JSONB().with_variant(sa.JSON(), "sqlite")

# Dialect-portable UUID type (native UUID on PostgreSQL, CHAR(32)/Uuid on SQLite)
PortableUUID = sa.Uuid(as_uuid=True).with_variant(PG_UUID(as_uuid=True), "postgresql")


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
