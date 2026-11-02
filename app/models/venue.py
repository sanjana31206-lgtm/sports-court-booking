from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Venue(Base):
    __tablename__ = "venues"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    address: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )