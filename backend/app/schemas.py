"""Pydantic 请求/响应模型。"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class ProviderBase(BaseModel):
    name: str
    api_base: str
    api_type: str = Field(pattern="^(anthropic|openai)$")
    native_responses: bool = False  # 原生支持 OpenAI Responses，Codex 可直连无需本地代理
    is_custom: bool = False


class ProviderCreate(ProviderBase):
    pass


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    api_base: Optional[str] = None
    api_type: Optional[str] = Field(default=None, pattern="^(anthropic|openai)$")
    native_responses: Optional[bool] = None
    is_custom: Optional[bool] = None


class ProviderOut(ProviderBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ConfigBase(BaseModel):
    name: str
    provider_id: int
    api_key: str = Field(..., description="明文 Key，后端加密存储")
    model: str
    targets: str = "claude"  # claude / codex / dsh / zcode / pi，逗号分隔可多选
    max_tokens: Optional[int] = Field(default=None, ge=1, description="dsh 单次输出上限；不填用兜底值")


class ConfigCreate(ConfigBase):
    pass


class ConfigUpdate(BaseModel):
    name: Optional[str] = None
    provider_id: Optional[int] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    targets: Optional[str] = None
    max_tokens: Optional[int] = Field(default=None, ge=1)


class ConfigOut(BaseModel):
    id: int
    name: str
    provider_id: int
    model: str
    targets: str = "claude"
    # max_tokens 原为 dsh 单次输出上限，v0.5.1 移除 dsh 接入后已无用。
    # 保留字段与库中数据（不做破坏性迁移），但不再对外暴露/使用。
    max_tokens: Optional[int] = None
    is_active: bool
    created_at: datetime
    provider: Optional[ProviderOut] = None

    model_config = {"from_attributes": True}


class SwitchRequest(BaseModel):
    restart: bool = False


class SwitchResult(BaseModel):
    success: bool
    message: str
    backup_path: Optional[str] = None
    codex_backup_path: Optional[str] = None
    restart: bool = False
    process_info: Optional[dict] = None


class StatusOut(BaseModel):
    version: Optional[str] = None
    active_config: Optional[ConfigOut] = None
    settings_file_exists: bool
    current_model: Optional[str] = None
    codex_file_exists: bool = False
    current_codex_model: Optional[str] = None
    current_codex_provider: Optional[str] = None


# ---------------- Skill 管理 ----------------


class SkillPathIn(BaseModel):
    path: str


class ProjectRootsIn(BaseModel):
    roots: list[str]


class IgnoredProjectsIn(BaseModel):
    """自动发现项目里的忽略名单：不想在项目视图看到的目录。"""

    paths: list[str]


class SkillSourceIn(BaseModel):
    scope: str = Field(pattern="^(global|project)$")
    tool: str = Field(pattern="^(claude|codex|zcode|qoder|dsh|pi|opencode|workbuddy)$")
    project: Optional[str] = None
    name: str  # frontmatter name 或目录名


class SkillTargetIn(BaseModel):
    scope: str = Field(pattern="^(global|project)$")
    tool: str = Field(pattern="^(claude|codex|zcode|qoder|dsh|pi|opencode|workbuddy)$")
    project: Optional[str] = None
    # 自选项目文件夹（绝对路径）：提供时优先于 project 名，.{tool}/skills 缺失会自动创建
    project_root: Optional[str] = None


class SkillMigrateIn(BaseModel):
    sources: list[SkillSourceIn]
    targets: list[SkillTargetIn]


# ---------------- MCP 管理 ----------------


class McpSourceIn(BaseModel):
    scope: str = Field(pattern="^(global|project)$")
    tool: str = Field(pattern="^(claude|codex|zcode|qoder|dsh|pi|opencode|workbuddy)$")
    project: Optional[str] = None
    name: str  # server 名


class McpTargetIn(BaseModel):
    scope: str = Field(pattern="^(global|project)$")
    tool: str = Field(pattern="^(claude|codex|zcode|qoder|dsh|pi|opencode|workbuddy)$")
    project: Optional[str] = None


class McpMigrateIn(BaseModel):
    sources: list[McpSourceIn]
    targets: list[McpTargetIn]


class McpServerRefIn(BaseModel):
    """定位某端某个 server（删除 / 详情）。"""

    scope: str = Field(pattern="^(global|project)$")
    tool: str = Field(pattern="^(claude|codex|zcode|qoder|dsh|pi|opencode|workbuddy)$")
    project: Optional[str] = None
    name: str


class McpDescriptionIn(BaseModel):
    """保存/清空某个 MCP server 的自定义功能介绍（description 空串即删除）。"""

    name: str
    description: str = ""


class McpServerCreate(BaseModel):
    """向指定端安装（新建）一个 MCP server。"""

    scope: str = Field(pattern="^(global|project)$")
    tool: str = Field(pattern="^(claude|codex|zcode|qoder|dsh|pi|opencode|workbuddy)$")
    project: Optional[str] = None
    name: str
    transport: str = Field(pattern="^(stdio|http|sse)$")
    command: Optional[str] = None
    args: list[str] = []
    env: dict[str, str] = {}
    url: Optional[str] = None
    headers: dict[str, str] = {}
    cwd: Optional[str] = None
    description: str = ""


class McpImportIn(BaseModel):
    """粘贴 JSON 配置片段批量安装（兼容 mcpServers 顶层 / server 字典 / 单对象）。"""

    scope: str = Field(pattern="^(global|project)$")
    tool: str = Field(pattern="^(claude|codex|zcode|qoder|dsh|pi|opencode|workbuddy)$")
    project: Optional[str] = None
    config_json: str


# ---------------- 无头任务 ----------------


class SkillRef(BaseModel):
    """任务/循环所选技能的一条引用（字段与方案 §4.2 逐字一致，契约冻结于 A1）。

    source_path 优先用；失效时后端按 name + source_tool + source_scope 重新定位。
    """

    name: str = Field(min_length=1, max_length=200)
    source_tool: str = ""
    source_scope: str = ""  # global / project
    source_project: str = ""
    source_path: str = ""  # 优先用；失效时按 name + tool + scope 重新定位


class TaskCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    tool: str = Field(pattern="^(claude|codex|zcode|qoder|dsh|pi|opencode)$", description="无头引擎")
    working_dir: Optional[str] = None
    timeout_seconds: Optional[int] = Field(default=None, ge=30, le=3600)
    # 上限 20：每个 skill 的描述都要占模型上下文，兼给注入/回收耗时兜底（§4.2）
    skills: list[SkillRef] = Field(default_factory=list, max_length=20)


class TaskOut(BaseModel):
    id: int
    prompt: str
    tool: str
    tool_label: str
    working_dir: str = ""
    timeout_seconds: int
    status: str
    model: str = ""
    config_id: Optional[int] = None
    exit_code: Optional[int] = None
    error_summary: str = ""
    output_path: str = ""
    precheck: dict = {}
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: Optional[int] = None
    loop_id: Optional[int] = None  # 非空 = 循环任务触发的一次运行
    trigger_type: str = ""  # auto=调度触发 / manual=手动立即执行；普通任务为空
    skills: list[SkillRef] = []


class TaskDetail(TaskOut):
    output: str = ""
    output_truncated: bool = False
    # 回收依据摘要（目标根 / 已建条目 / 跳过项），仅详情回传，便于排查残留
    skill_links: dict = {}


# ---------------- 循环任务 ----------------

class LoopBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    tool: str = Field(pattern="^(claude|codex|zcode|qoder|dsh|pi|opencode)$")
    task_content: str = Field(min_length=1, max_length=8000)
    workdir: str = ""
    schedule_type: str = Field(pattern="^(once|interval|daily|weekly)$")
    # dict 或 JSON 字符串都吃：服务端用 loop_schedule.parse_config 校验并规范化
    schedule_config: Any = {}
    timeout_seconds: int = Field(default=1800, ge=300, le=14400)
    catch_up: bool = False
    enabled: bool = True
    # 与任务下发同一份快照结构与上限（循环触发时复制进 AgentTask.skills，见批 4）
    skills: list[SkillRef] = Field(default_factory=list, max_length=20)


class LoopCreate(LoopBase):
    pass


class LoopUpdate(LoopBase):
    pass


class LoopOut(BaseModel):
    id: int
    name: str
    tool: str
    tool_label: str
    task_content: str
    workdir: str = ""
    schedule_type: str
    schedule_config: dict = {}
    timeout_seconds: int
    catch_up: bool
    enabled: bool
    status: str
    next_run_at: str = ""
    last_run_at: str = ""
    running: bool = False
    last_run: Optional[dict] = None
    skills: list[SkillRef] = []


# ---------------- 应用内反馈 ----------------

class FeedbackSubmitted(BaseModel):
    """反馈提交结果：本地留档 id + 邮件是否送达。"""

    id: int
    emailed: bool


# ---------------- 手机通知 ----------------

class PhoneEventsIn(BaseModel):
    success: Optional[bool] = None
    failed: Optional[bool] = None
    timeout: Optional[bool] = None
    cancelled: Optional[bool] = None


class PhoneConfigIn(BaseModel):
    """部分更新：未传字段保持原值；token 传空串即清除。"""

    enabled: Optional[bool] = None
    server: Optional[str] = Field(default=None, max_length=300)
    topic: Optional[str] = Field(default=None, max_length=64)
    token: Optional[str] = Field(default=None, max_length=256)
    events: Optional[PhoneEventsIn] = None
    desktop: Optional[bool] = None


class PhoneConfigOut(BaseModel):
    enabled: bool
    server: str
    topic: str
    token_set: bool
    events: dict
    subscribe_url: str
    qr_payload: str
    desktop: bool


class PhoneTestOut(BaseModel):
    """测试推送结果：失败原因原样回给前端前置弹窗，不静默。"""

    sent: bool
    detail: str = ""


# ---------------- 会话交互（hook） ----------------

class PhoneHookIn(BaseModel):
    """部分更新：未传字段保持原值。"""

    mode: Optional[str] = Field(default=None, pattern="^(out|home)$")
    timeout: Optional[int] = Field(default=None, ge=15, le=600)
    plan_timeout: Optional[int] = Field(default=None, ge=30, le=1800)


class PhoneHookStatusOut(BaseModel):
    mode: str
    timeout: int
    plan_timeout: int
    topic_ready: bool
    hook_command: str
    engines: dict
    desktop: bool


class HookEngineIn(BaseModel):
    engine: str
