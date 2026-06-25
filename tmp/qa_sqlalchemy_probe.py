from pathlib import Path

from sqlalchemy import create_engine, text


def main() -> None:
    db_path = Path("tmp/qa_sqlalchemy_probe.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        conn.execute(text("create table if not exists t(id integer primary key, note text)"))
        conn.execute(text("insert into t(note) values ('ok')"))
    with engine.connect() as conn:
        print(conn.execute(text("select count(*) from t")).scalar_one())


if __name__ == "__main__":
    main()
