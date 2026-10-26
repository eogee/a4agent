"""SQLAlchemy 数据模型。"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import relationship

from .database import Base


class Provider(Base):
    __tablename__ = "providers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    api_base = Column(String(500), nullable=False)
    api_type = Column(String(20), nullable=False)  # anthropic / openai
    native_responses = Column(Boolean, default=False)  # OpenAI 兼容且原生支持 /responses，Codex 可直连
    is_custom = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


class Configuration(Base):
    __tablename__ = "configurations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False)
    api_key_encrypted = Column(Text, nullable=False)  # DPAPI 加密后的 base64
    model = Column(String(100), nullable=False)
    targets = Column(String(50), nullable=False, default="claude")  # claude / codex / dsh，逗号分隔可多选
    max_tokens = Column(Integer, nullable=True)  # dsh 单次输出上限；None 时用兜底值（131072）
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)

    provider = relationship("Provider", lazy="joined")


class SwitchLog(Base):
    __tablename__ = "switch_logs"

    id = Column(Integer, primary_key=True, index=True)
    config_id = Column(Integer, nullable=True)
    switch_time = Column(DateTime, default=datetime.now)
    status = Column(String(20), nullable=False)  # success / failed
    detail = Column(Text, default="")


class Backup(Base):
    __tablename__ = "backups"

    id = Column(Integer, primary_key=True, index=True)
    config_id = Column(Integer, nullable=True)
    backup_time = Column(DateTime, default=datetime.now)
    file_path = Column(String(500), nullable=False)


class SkillMigration(Base):
    """Skill 迁移日志（复制语义，一次 source×target 一条）。"""

    __tablename__ = "skill_migrations"

    id = Column(Integer, primary_key=True, index=True)
    skill_name = Column(String(200), nullable=False)
    source_tool = Column(String(20), nullable=False)  # claude / codex / dsh / zcode
    source_scope = Column(String(10), nullable=False)  # global / project
    source_project = Column(String(200), nullable=True)
    source_path = Column(String(500), nullable=False)
    target_tool = Column(String(20), nullable=False)
    target_scope = Column(String(10), nullable=False)
    target_project = Column(String(200), nullable=True)
    status = Column(String(20), nullable=False)  # success / failed
    detail = Column(Text, default="")
    migrate_time = Column(DateTime, default=datetime.now)


class SkillTrash(Base):
    """Skill 回收站条目；超 30 天惰性清理，可恢复原位。"""

    __tablename__ = "skill_trash"

    id = Column(Integer, primary_key=True, index=True)
    skill_name = Column(String(200), nullable=False)  # frontmatter name
    dir_name = Column(String(200), nullable=False)  # 目录名（回收站目录前缀）
    tool = Column(String(20), nullable=True)  # 来源端 claude / codex / dsh / zcode
    scope = Column(String(10), nullable=True)  # global / project
    project = Column(String(200), nullable=True)  # 项目级来源项目名
    original_path = Column(String(500), nullable=False)  # 原位置（恢复目标）
    trash_path = Column(String(500), nullable=False)  # 回收站中的当前路径
    trash_time = Column(DateTime, default=datetime.now)


class McpMigration(Base):
    """MCP server 迁移日志（写入语义：源端配置片段复制到目标配置文件）。"""

    __tablename__ = "mcp_migrations"

    id = Column(Integer, primary_key=True, index=True)
    server_name = Column(String(200), nullable=False)
    transport = Column(String(30), default="")  # stdio / sse / streamable-http / http
    source_tool = Column(String(20), nullable=False)  # claude / codex / dsh
    source_scope = Column(String(10), nullable=False)  # global / project
    source_project = Column(String(200), nullable=True)
    source_path = Column(String(500), nullable=False)
    target_tool = Column(String(20), nullable=False)
    target_scope = Column(String(10), nullable=False)
    target_project = Column(String(200), nullable=True)
    status = Column(String(20), nullable=False)  # success / failed
    detail = Column(Text, default="")
    migrate_time = Column(DateTime, default=datetime.now)


class McpTrash(Base):
    """MCP 回收站条目：被替换/删除的 server 配置片段快照（含 DPAPI 加密的 env）。"""

    __tablename__ = "mcp_trash"

    id = Column(Integer, primary_key=True, index=True)
    server_name = Column(String(200), nullable=False)
    tool = Column(String(20), nullable=True)  # 来源端 claude / codex / dsh
    scope = Column(String(10), nullable=True)  # global / project
    project = Column(String(200), nullable=True)
    original_path = Column(String(500), nullable=False)  # 原配置文件路径（恢复目标）
    trash_path = Column(String(500), nullable=False)  # 回收站中的快照 json 路径
    trash_time = Column(DateTime, default=datetime.now)


class Feedback(Base):
    """应用内反馈留档：Bug 报告 / 功能需求，直接邮件送达开发者（eogee@qq.com）。

    反馈先落本地库再尽力发信（emailed 标志记录送达结果），截图随行存
    blob（≤10 张 × ≤1MB，同 EoListen），邮件以附件形式把截图带给开发者。
    """

    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(10), nullable=False)  # bug / feature
    content = Column(Text, nullable=False)  # 问题描述（用户原文）
    contact = Column(String(200), default="")  # 联系方式（选填）
    env_info = Column(Text, default="")  # 提交时采集的环境信息（多行文本）
    log_excerpt = Column(Text, default="")  # 用户勾选附带的日志尾部片段
    image_count = Column(Integer, default=0)
    emailed = Column(Boolean, default=False)  # 邮件是否发送成功
    created_at = Column(DateTime, default=datetime.now)


class FeedbackImage(Base):
    """反馈截图：seq 为弹窗内的展示序号（1 起），blob 上限 1MB 见 api/v1/feedback.py。"""

    __tablename__ = "feedback_images"

    id = Column(Integer, primary_key=True, index=True)
    feedback_id = Column(Integer, ForeignKey("feedback.id"), nullable=False)
    seq = Column(Integer, nullable=False)
    filename = Column(String(200), default="")
    mime = Column(String(50), default="image/png")
    data = Column(LargeBinary, nullable=False)


class AgentTask(Base):
    """无头任务：下发即入库，后台线程执行，产出落文件（路径见 output_path）。"""

    __tablename__ = "agent_tasks"

    id = Column(Integer, primary_key=True, index=True)
    prompt = Column(Text, nullable=False)
    tool = Column(String(20), nullable=False)  # 无头引擎 pi / dsh
    working_dir = Column(String(500), default="")
    timeout_seconds = Column(Integer, default=600)
    # pending / running / success / failed / timeout / cancelled / precheck_failed
    status = Column(String(20), nullable=False, default="pending")
    config_id = Column(Integer, nullable=True)  # 下发时生效的配置方案
    model = Column(String(100), default="")
    exit_code = Column(Integer, nullable=True)
    error_summary = Column(Text, default="")
    output_path = Column(String(500), default="")
    precheck_result = Column(Text, default="")  # 预检明细 JSON（步骤/耗时/失败原因）
    precheck_ms = Column(Integer, nullable=True)
    # 循环任务的运行记录也落这张表：非空 loop_id 即「由循环调度触发的那一次运行」。
    # 执行器/日志/取消/通知/详情全部复用任务链，循环侧只多这两列来源标记。
    loop_id = Column(Integer, nullable=True, index=True)
    trigger_type = Column(String(10), default="")  # auto / manual；普通任务为空
    created_at = Column(DateTime, default=datetime.now)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class Loop(Base):
    """循环任务：到点由调度线程拉起目标 CLI 的无头模式执行。

    本表只描述「什么时候、用谁、做什么」；每次执行的运行记录是一条带
    loop_id 的 AgentTask，不另建运行表（见 AgentTask.loop_id）。
    schedule_config 是 JSON 字符串，结构与 next_run 计算同处
    app.loop_schedule，写入前由 API 层校验。
    """

    __tablename__ = "loops"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    tool = Column(String(20), nullable=False)
    task_content = Column(Text, nullable=False)
    workdir = Column(String(500), default="")
    schedule_type = Column(String(20), nullable=False)  # once / interval / daily / weekly
    schedule_config = Column(String(500), default="{}")  # {"run_at"}/{"minutes"}/{"time"}/{"weekdays":[],"time"}
    timeout_seconds = Column(Integer, default=1800)  # 默认 30 分钟，5 分钟 ~ 4 小时
    catch_up = Column(Boolean, default=False)  # 启动时是否补跑错过的触发
    enabled = Column(Boolean, default=True)
    status = Column(String(20), default="active")  # active / paused / completed
    next_run_at = Column(DateTime, nullable=True, index=True)  # 调度扫描依据
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
