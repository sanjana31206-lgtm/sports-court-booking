from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
    )