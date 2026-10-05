"""SQLite 数据库连接与会话管理。"""
import os
import shutil
import stat
import sys
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .env_compat import env_first

DATA_DIR_NAME = "a4agent"
DB_NAME = "a4agent.db"
# 历代遗留数据目录/库名（品牌改名链：a4agent ← a4api ← api-switch），启动时自动迁移
LEGACY_STORES = [
    ("a4api", "a4api.db"),  # v0.3.x 及更早（曾用名 a4api）
    ("api-switch", "api_switch.db"),  # 最早期（曾用名 api-switch）
]
# 整目录搬迁失败时的兜底拷贝项（不拷 llama 引擎等大目录，丢失时用户重新下载即可）
_COPY_ITEM_NAMES = ("backups", "proxy.json", "llama_config.json", "update_state.json", "skills_recycle")


def _restrict_permissions(path: Path) -> None:
    """尽力把数据目录/数据库文件权限收紧为仅当前用户。

    Windows 上 chmod 不改变 ACL，主要由 APPDATA/项目目录的 NTFS 权限决定；
    这里对非 Windows 环境仍做 700/600 收紧，属于尽力而为的加固。
    """
    if os.name == "nt":
        return
    try:
        if path.is_dir():
            os.chmod(path, stat.S_IRWXU)
        else:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _legacy_data_dir(legacy_dir_name: str) -> Path:
    """某一代遗留品牌的运行时数据目录。"""
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / legacy_dir_name
    return Path(__file__).resolve().parent.parent / "database"  # backend/database


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def _copy_missing(old_dir: Path, new_dir: Path) -> None:
    """把旧目录里新目录缺失的关键小文件补拷过来。"""
    try:
        for name in _COPY_ITEM_NAMES:
            src, dst = old_dir / name, new_dir / name
            if dst.exists() or not src.exists():
                continue
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
    except OSError:
        pass  # 迁移失败不阻塞启动，仍使用新目录


def _migrate_db_file(new_dir: Path, legacy_db: Path) -> None:
    """旧库名 → 新库名（a4api.db → a4agent.db）。同目录用改名，跨目录用拷贝。"""
    if (new_dir / DB_NAME).exists():
        return
    for src in (new_dir / legacy_db.name, legacy_db):
        if not src.exists():
            continue
        try:
            if src.parent == new_dir:
                src.rename(new_dir / DB_NAME)
            else:
                shutil.copy2(src, new_dir / DB_NAME)
        except OSError:
            pass
        return


def _migrate_legacy_data(new_dir: Path) -> None:
    """一次性迁移历代遗留（a4api / api-switch）运行时数据，避免改名后数据“丢失”。

    优先整目录改名（同卷瞬时完成，llama 引擎等大文件一并带走）；目标已有数据或
    被占用导致改名失败时，退化为只补拷关键小文件。开发态新旧目录同为
    backend/database，只需处理库文件改名。
    """
    for legacy_dir_name, legacy_db_name in LEGACY_STORES:
        old_dir = _legacy_data_dir(legacy_dir_name)
        if not old_dir.exists():
            continue
        same = _same_path(old_dir, new_dir)
        if same:  # 开发态：新旧目录相同，只处理库文件改名
            _migrate_db_file(new_dir, new_dir / legacy_db_name)
            continue
        if not any(new_dir.iterdir()):
            # 整目录搬迁：同卷 rename 瞬时完成，llama 引擎等大文件一并带走。
            # Windows 上目标已存在（哪怕为空）会让 rename 失败，先摘掉空壳；
            # rmdir 成功而 rename 失败时必须把新目录补建回来，再走拷贝兜底。
            try:
                new_dir.rmdir()
                old_dir.rename(new_dir)
                _migrate_db_file(new_dir, new_dir / legacy_db_name)
                continue
            except OSError:
                pass
            new_dir.mkdir(parents=True, exist_ok=True)
        _copy_missing(old_dir, new_dir)
        _migrate_db_file(new_dir, old_dir / legacy_db_name)


def get_data_dir() -> Path:
    """运行时数据目录：打包后写入用户目录，开发时用 backend/database。"""
    override = env_first("A4AGENT_DATA_DIR", "A4API_DATA_DIR")
    if override:
        base = Path(override)
    elif getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA", Path.home())) / DATA_DIR_NAME
    else:
        base = Path(__file__).resolve().parent.parent / "database"  # backend/database
    base.mkdir(parents=True, exist_ok=True)
    _restrict_permissions(base)
    _migrate_legacy_data(base)
    return base


DATABASE_PATH = get_data_dir() / DB_NAME
DATABASE_URL = f"sqlite:///{DATABASE_PATH.as_posix()}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


def ensure_schema() -> None:
    """轻量迁移：为旧库补充新列 / 补建新表（SQLAlchemy create_all 不会修改已存在的表）。"""
    from sqlalchemy import text

    # Skill 管理两张表的补建：对旧库（create_all 时模型尚不存在的情况）兜底
    from .models import (AgentTask, McpMigration, McpTrash, SkillMigration,
                         SkillTrash)

    Base.metadata.create_all(
        bind=engine,
        tables=[
            SkillMigration.__table__,
            SkillTrash.__table__,
            McpMigration.__table__,
            McpTrash.__table__,
            AgentTask.__table__,
        ],
    )

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(configurations)"))}
        if "targets" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE configurations "
                    "ADD COLUMN targets VARCHAR(50) NOT NULL DEFAULT 'claude'"
                )
            )
        if "max_tokens" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE configurations "
                    "ADD COLUMN max_tokens INTEGER"
                )
            )
        pcols = {row[1] for row in conn.execute(text("PRAGMA table_info(providers)"))}
        if "native_responses" not in pcols:
            conn.execute(
                text(
                    "ALTER TABLE providers "
                    "ADD COLUMN native_responses BOOLEAN NOT NULL DEFAULT 0"
                )
            )

        # 反馈表：v0.3.1 起改为「直接邮件送达」结构（截图/环境/日志/送达标志）；
        # 旧库的 issue_text / channel 列不再使用，SQLite 不便删列，保留不碍事
        fcols = {row[1] for row in conn.execute(text("PRAGMA table_info(feedback)"))}
        if fcols:  # 表已存在（旧结构）→ 补新列；新库由 create_all 直接建全
            if "env_info" not in fcols:
                conn.execute(text("ALTER TABLE feedback ADD COLUMN env_info TEXT NOT NULL DEFAULT ''"))
            if "log_excerpt" not in fcols:
                conn.execute(text("ALTER TABLE feedback ADD COLUMN log_excerpt TEXT NOT NULL DEFAULT ''"))
            if "image_count" not in fcols:
                conn.execute(text("ALTER TABLE feedback ADD COLUMN image_count INTEGER NOT NULL DEFAULT 0"))
            if "emailed" not in fcols:
                conn.execute(text("ALTER TABLE feedback ADD COLUMN emailed BOOLEAN NOT NULL DEFAULT 0"))


@event.listens_for(engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """SQLite 默认不启用外键约束，这里按连接开启，避免产生孤儿数据。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@event.listens_for(engine, "connect")
def _restrict_db_file_permissions(dbapi_connection, connection_record):
    """数据库文件创建/连接后尽力收紧权限。"""
    _restrict_permissions(DATABASE_PATH)


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI 依赖：请求级数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
