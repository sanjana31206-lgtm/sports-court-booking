from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Court(Base):
    __tablename__ = "courts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    sport_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    venue_id: Mapped[int] = mapped_column(
        ForeignKey("venues.id"),
        nullable=False,
    )