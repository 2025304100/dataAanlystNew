from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Table


class Base(DeclarativeBase):
    @classmethod
    def __table_cls__(cls, *args, **kwargs):
        # Direct metadata.create_all must be transactional even when the
        # MySQL server's default_storage_engine is MyISAM.
        kwargs.setdefault("mysql_engine", "InnoDB")
        return Table(*args, **kwargs)

