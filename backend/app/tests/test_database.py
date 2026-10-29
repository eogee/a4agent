"""数据目录迁移（品牌改名 a4api → a4agent）测试。"""
from sqlalchemy import create_engine, text

from backend.app import database


def test_migrate_whole_dir_rename(tmp_path, monkeypatch):
    """打包态典型路径：旧 %APPDATA%\\a4api 整目录改名迁入 a4agent，旧库名同步替换。"""
    old_dir = tmp_path / "a4api"
    (old_dir / "backups").mkdir(parents=True)
    (old_dir / "a4api.db").write_bytes(b"db")
    (old_dir / "proxy.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(database, "_legacy_data_dir", lambda name: tmp_path / name)

    new_dir = tmp_path / "a4agent"
    new_dir.mkdir()  # get_data_dir 会先建空目录
    database._migrate_legacy_data(new_dir)

    assert not old_dir.exists()  # 整目录已改名
    assert (new_dir / "a4agent.db").read_bytes() == b"db"
    assert (new_dir / "backups").is_dir()
    assert (new_dir / "proxy.json").exists()


def test_migrate_copy_fallback_when_target_has_data(tmp_path, monkeypatch):
    """新目录已有数据时退化为逐项拷贝，旧目录保留原样。"""
    old_dir = tmp_path / "a4api"
    old_dir.mkdir()
    (old_dir / "a4api.db").write_bytes(b"db")
    (old_dir / "proxy.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(database, "_legacy_data_dir", lambda name: tmp_path / name)

    new_dir = tmp_path / "a4agent"
    (new_dir / "updates").mkdir(parents=True)  # 非空 → 不做整目录改名
    database._migrate_legacy_data(new_dir)

    assert old_dir.exists()
    assert (new_dir / "a4agent.db").read_bytes() == b"db"
    assert (new_dir / "proxy.json").exists()


def test_migrate_dev_mode_db_file_rename(tmp_path, monkeypatch):
    """开发态新旧目录同为 backend/database：只做库文件改名 a4api.db → a4agent.db。"""
    data_dir = tmp_path / "database"
    data_dir.mkdir()
    (data_dir / "a4api.db").write_bytes(b"db")
    monkeypatch.setattr(database, "_legacy_data_dir", lambda name: data_dir)

    database._migrate_legacy_data(data_dir)

    assert (data_dir / "a4agent.db").read_bytes() == b"db"
    assert not (data_dir / "a4api.db").exists()


def test_migrate_noop_when_nothing_legacy(tmp_path, monkeypatch):
    """无遗留目录时迁移为零操作，不报错。"""
    monkeypatch.setattr(database, "_legacy_data_dir", lambda name: tmp_path / name)
    new_dir = tmp_path / "a4agent"
    new_dir.mkdir()
    database._migrate_legacy_data(new_dir)
    assert new_dir.is_dir()
    assert not (new_dir / "a4agent.db").exists()


def test_ensure_schema_adds_skill_columns_to_old_db(tmp_path, monkeypatch):
    """旧库（无 skills / skill_links / loops.skills）跑一次 ensure_schema 即补齐三列。

    手工建最小旧表再补列，是「create_all 不会修改已存在表」的唯一有效回归路径。
    configurations 也要有（_migrate_targets_v2 会扫它），否则会误报连接错误。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE agent_tasks ("
            "id INTEGER PRIMARY KEY, prompt TEXT NOT NULL, tool VARCHAR(20) NOT NULL,"
            "status VARCHAR(20) NOT NULL)"
        ))
        conn.execute(text("CREATE TABLE loops (id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL)"))
        conn.execute(text("CREATE TABLE configurations (id INTEGER PRIMARY KEY, targets VARCHAR(50))"))
        conn.execute(text("CREATE TABLE providers (id INTEGER PRIMARY KEY, name VARCHAR(100))"))
    monkeypatch.setattr(database, "engine", engine)

    database.ensure_schema()

    tcols = {row[1] for row in engine.connect().execute(text("PRAGMA table_info(agent_tasks)"))}
    assert {"skills", "skill_links"} <= tcols
    lcols = {row[1] for row in engine.connect().execute(text("PRAGMA table_info(loops)"))}
    assert "skills" in lcols


def test_ensure_schema_is_idempotent_on_fresh_db(tmp_path, monkeypatch):
    """新库（create_all 已建全）再跑一次不得重复 ALTER 报错。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'new.db'}")
    monkeypatch.setattr(database, "engine", engine)
    database.Base.metadata.create_all(bind=engine)

    database.ensure_schema()
    database.ensure_schema()

    tcols = {row[1] for row in engine.connect().execute(text("PRAGMA table_info(agent_tasks)"))}
    assert {"skills", "skill_links"} <= tcols
