from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, StrictInt

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / ".data"
DATA_DIR.mkdir(exist_ok=True)
load_dotenv(ROOT / ".env")
DB_PATH = Path(os.getenv("DEVTING_DB_PATH", str(DATA_DIR / "zhidian.sqlite3"))).expanduser()
if os.getenv("DEEPSEEK_API_KEY"):
    # LangChain's OpenAI-compatible integration reads these standard variables.
    os.environ["OPENAI_API_KEY"] = os.environ["DEEPSEEK_API_KEY"]
    os.environ["OPENAI_BASE_URL"] = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

ROLE_INFO: dict[str, dict[str, str]] = {
    "pdt": {"name": "PDT 经理", "short": "PDT", "color": "#7666db", "summary": "负责用户沟通、方向和产品决策。"},
    "pm": {"name": "项目经理", "short": "PM", "color": "#4388c9", "summary": "负责计划、依赖、风险和交付物跟踪。"},
    "se": {"name": "系统工程师", "short": "SE", "color": "#2b9c91", "summary": "负责系统设计、技术决策和开发方向协调。"},
    "pe": {"name": "产品工程师", "short": "PE", "color": "#d58c3f", "summary": "负责需求澄清、PRD、范围和验收定义。"},
    "ui": {"name": "UI 工程师", "short": "UI", "color": "#bf6b97", "summary": "负责用户流程、交互和视觉设计交付。"},
    "fe": {"name": "前端工程师", "short": "FE", "color": "#5985d3", "summary": "负责客户端和 Web 交付。"},
    "be": {"name": "后端工程师", "short": "BE", "color": "#56755f", "summary": "负责服务端、API 和数据接口。"},
    "data": {"name": "大数据工程师", "short": "DE", "color": "#a78643", "summary": "负责数据平台、指标和数据质量。"},
    "algo": {"name": "算法工程师", "short": "AI", "color": "#765ab6", "summary": "负责算法方案、模型能力和评估。"},
    "qa": {"name": "测试工程师", "short": "QA", "color": "#c66e61", "summary": "负责测试策略、测试大纲和质量验收。"},
    "ops": {"name": "部署运维工程师", "short": "OPS", "color": "#71809a", "summary": "负责环境、流水线、发布和运维。"},
}
ROLE_ORDER = ["pm", "se", "pe", "ui", "fe", "be", "data", "algo", "qa", "ops"]
EVIDENCE_LEVELS = {"E0", "E1", "E2", "E3", "E4"}
STORE_LOCK = threading.RLock()
AGENT_CACHE: dict[tuple[str, str, str, str], Any] = {}
MEETING_LOCK: asyncio.Lock | None = None
PLAN_LOCK: asyncio.Lock | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def synchronized(function: Any) -> Any:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with STORE_LOCK:
            return function(*args, **kwargs)
    return wrapper


class ProjectStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS project_state (
                    project_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS project_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, actor TEXT NOT NULL, summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    product_shape TEXT NOT NULL DEFAULT '', development_mode TEXT NOT NULL DEFAULT '敏捷迭代',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS document_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL, document_id TEXT NOT NULL,
                    version TEXT NOT NULL, actor TEXT NOT NULL, summary TEXT NOT NULL,
                    content_json TEXT NOT NULL, created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_document_versions_project_doc ON document_versions(project_id,document_id,id DESC)"
            )
            existing = connection.execute(
                "SELECT project_id FROM projects WHERE project_id = 'default'"
            ).fetchone()
            if not existing:
                legacy = connection.execute(
                    "SELECT state_json FROM project_state WHERE project_id = 'default'"
                ).fetchone()
                legacy_state = json.loads(legacy["state_json"]) if legacy else {}
                stamp = now_iso()
                connection.execute(
                    "INSERT INTO projects(project_id,name,description,product_shape,development_mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    ("default", legacy_state.get("projectName", "我的项目"), legacy_state.get("projectDescription", ""), legacy_state.get("productShape", "待确定"), legacy_state.get("mode", "敏捷迭代"), stamp, stamp),
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def load(self, project_id: str = "default") -> dict[str, Any] | None:
        with STORE_LOCK, self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM project_state WHERE project_id = ?", (project_id,)
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def save(self, state: dict[str, Any], project_id: str = "default", snapshot_actor: str | None = None) -> None:
        serialized = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        with STORE_LOCK, self._connect() as connection:
            previous_row = connection.execute(
                "SELECT state_json FROM project_state WHERE project_id = ?", (project_id,)
            ).fetchone()
            previous_state = json.loads(previous_row["state_json"]) if previous_row else {}
            previous_documents = {
                str(document.get("id")): document
                for document in previous_state.get("docs", []) if document.get("id")
            }
            connection.execute(
                """INSERT INTO project_state(project_id, state_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(project_id) DO UPDATE SET
                   state_json=excluded.state_json, updated_at=excluded.updated_at""",
                (project_id, serialized, now_iso()),
            )
            connection.execute("UPDATE projects SET updated_at=? WHERE project_id=?", (now_iso(), project_id))
            for document in state.get("docs", []):
                document_id = str(document.get("id") or "")
                if not document_id:
                    continue
                previous = previous_documents.get(document_id)
                if previous is not None and previous == document:
                    continue
                connection.execute(
                    """INSERT INTO document_versions(project_id,document_id,version,actor,summary,content_json,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (project_id, document_id, str(document.get("version", "v0.1")),
                     snapshot_actor or str(document.get("owner") or "system"),
                     str(document.get("lastChange") or ("初始文档骨架" if previous is None else "文档内容或状态更新")),
                     json.dumps(document, ensure_ascii=False, separators=(",", ":")), now_iso()),
                )

    def seed_document_baselines(self) -> None:
        """Create one immutable baseline for documents saved before snapshot support existed."""
        with STORE_LOCK, self._connect() as connection:
            projects = connection.execute("SELECT project_id,state_json FROM project_state").fetchall()
            for row in projects:
                project_id = row["project_id"]
                state = json.loads(row["state_json"])
                for document in state.get("docs", []):
                    document_id = str(document.get("id") or "")
                    if not document_id:
                        continue
                    exists = connection.execute(
                        "SELECT 1 FROM document_versions WHERE project_id=? AND document_id=? LIMIT 1",
                        (project_id, document_id),
                    ).fetchone()
                    if exists:
                        continue
                    connection.execute(
                        """INSERT INTO document_versions(project_id,document_id,version,actor,summary,content_json,created_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (project_id, document_id, str(document.get("version", "v0.1")), "迁移基线",
                         "启用文档版本快照时保存的当前版本",
                         json.dumps(document, ensure_ascii=False, separators=(",", ":")), now_iso()),
                    )

    def document_versions(self, project_id: str, document_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with STORE_LOCK, self._connect() as connection:
            rows = connection.execute(
                """SELECT id,version,actor,summary,content_json,created_at FROM document_versions
                   WHERE project_id=? AND document_id=? ORDER BY id DESC LIMIT ?""",
                (project_id, document_id, limit),
            ).fetchall()
        versions = []
        for row in rows:
            version = dict(row)
            version["content"] = json.loads(version.pop("content_json"))
            versions.append(version)
        return versions

    def document_version(self, project_id: str, document_id: str, version_id: int) -> dict[str, Any] | None:
        with STORE_LOCK, self._connect() as connection:
            row = connection.execute(
                """SELECT id,version,actor,summary,content_json,created_at FROM document_versions
                   WHERE project_id=? AND document_id=? AND id=?""",
                (project_id, document_id, version_id),
            ).fetchone()
        if not row:
            return None
        version = dict(row)
        version["content"] = json.loads(version.pop("content_json"))
        return version

    def record_event(self, event_type: str, actor: str, summary: str, project_id: str = "default") -> None:
        with STORE_LOCK, self._connect() as connection:
            connection.execute(
                "INSERT INTO project_events(project_id,event_type,actor,summary,created_at) VALUES(?,?,?,?,?)",
                (project_id, event_type, actor, summary, now_iso()),
            )

    def events(self, project_id: str = "default", limit: int = 40) -> list[dict[str, Any]]:
        with STORE_LOCK, self._connect() as connection:
            rows = connection.execute(
                "SELECT id,event_type,actor,summary,created_at FROM project_events WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def events_after(self, project_id: str, event_id: int) -> list[dict[str, Any]]:
        with STORE_LOCK, self._connect() as connection:
            rows = connection.execute(
                "SELECT id,event_type,actor,summary,created_at FROM project_events WHERE project_id=? AND id>? ORDER BY id ASC LIMIT 100",
                (project_id, event_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_projects(self) -> list[dict[str, Any]]:
        with STORE_LOCK, self._connect() as connection:
            rows = connection.execute(
                "SELECT project_id,name,description,product_shape,development_mode,created_at,updated_at FROM projects ORDER BY updated_at DESC"
            ).fetchall()
        projects = []
        for row in rows:
            project = dict(row)
            state = self.load(project["project_id"]) or {}
            project.update({"requirement_count": len(state.get("requirements", [])), "task_count": len(state.get("tasks", []))})
            projects.append(project)
        return projects

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with STORE_LOCK, self._connect() as connection:
            row = connection.execute(
                "SELECT project_id,name,description,product_shape,development_mode,created_at,updated_at FROM projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_project(self, project_id: str) -> None:
        with STORE_LOCK, self._connect() as connection:
            connection.execute("DELETE FROM document_versions WHERE project_id=?", (project_id,))
            connection.execute("DELETE FROM project_events WHERE project_id=?", (project_id,))
            connection.execute("DELETE FROM project_state WHERE project_id=?", (project_id,))
            connection.execute("DELETE FROM projects WHERE project_id=?", (project_id,))

    def create_project(self, name: str, description: str, product_shape: str, development_mode: str) -> dict[str, Any]:
        project_id = uuid.uuid4().hex[:12]
        stamp = now_iso()
        project = {
            "project_id": project_id, "name": name, "description": description,
            "product_shape": product_shape, "development_mode": development_mode,
            "created_at": stamp, "updated_at": stamp,
        }
        with STORE_LOCK, self._connect() as connection:
            connection.execute(
                "INSERT INTO projects(project_id,name,description,product_shape,development_mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (project_id, name, description, product_shape, development_mode, stamp, stamp),
            )
        return project


store = ProjectStateStore(DB_PATH)
store.seed_document_baselines()


class ChatRequest(BaseModel):
    role: str = "group"
    message: str = Field(min_length=1, max_length=12000)
    project_id: str = "default"


class StatePayload(BaseModel):
    state: dict[str, Any]


class MeetingConfig(BaseModel):
    enabled: bool
    cadence: int = Field(default=5, ge=3, le=120)
    agenda: list[str] | None = Field(default=None, max_length=10)


class MeetingRunRequest(BaseModel):
    topic: str = Field(default="项目进展与阻塞", min_length=3, max_length=240)
    agenda: list[str] = Field(default_factory=list, max_length=10)


class ApprovalPayload(BaseModel):
    note: str = Field(min_length=3, max_length=2000)


class RequirementDecisionPayload(BaseModel):
    decision: Literal["已拒绝", "已搁置", "挂起待证据"]
    note: str = Field(min_length=3, max_length=2000)


class RequirementRevisionPayload(BaseModel):
    action_id: str = Field(min_length=8, max_length=80)
    title: str = Field(min_length=3, max_length=240)
    user_problem: str = Field(min_length=3, max_length=2000)
    rationality_assessment: str = Field(min_length=3, max_length=2000)
    value_hypothesis: str = Field(min_length=3, max_length=1000)
    evidence: str = Field(min_length=3, max_length=3000)
    evidence_gap: str = Field(min_length=3, max_length=2000)
    kano_candidate: Literal["魅力型", "期望型", "基本型", "无差异型", "反向型", "待分析"]
    classification_rationale: str = Field(min_length=3, max_length=3000)
    evidence_level: Literal["E0", "E1", "E2", "E3", "E4"] = "E0"
    priority: Literal["P0", "P1", "P2"] = "P1"
    confidence: int = Field(default=0, ge=0, le=100)
    note: str = Field(min_length=3, max_length=2000)


class ProjectFactCreateRequest(BaseModel):
    kind: Literal["事实", "决策", "证据", "假设", "风险"]
    title: str = Field(min_length=3, max_length=180)
    content: str = Field(min_length=3, max_length=4000)
    source: str = Field(min_length=3, max_length=1200)
    evidence_level: Literal["E0", "E1", "E2", "E3", "E4"] = "E1"
    requirement_id: str = Field(default="", max_length=80)
    task_id: str = Field(default="", max_length=80)
    document_id: str = Field(default="", max_length=80)


class ProjectFactReviewRequest(BaseModel):
    decision: Literal["用户确认", "已取代"]
    note: str = Field(default="", max_length=1200)
    change_summary: str = Field(default="", max_length=200)


class ProjectDecisionReviewRequest(BaseModel):
    decision: Literal["已采纳", "已拒绝", "已搁置"]
    note: str = Field(min_length=3, max_length=2000)


class TaskCreateRequest(BaseModel):
    requirement_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=240)
    assignee_role: str = Field(min_length=1, max_length=40)
    acceptance_criteria: str = Field(min_length=3, max_length=4000)
    due: str = Field(default="待排期", max_length=40)
    allow_parallel: bool = True


class TaskUpdateRequest(BaseModel):
    state: Literal["待开始", "进行中", "评审中", "已完成", "有阻塞"]
    note: str = Field(default="", max_length=3000)


class RoadmapSettingsRequest(BaseModel):
    max_parallel_tasks: int = Field(ge=1, le=32)


class TaskParallelismRequest(BaseModel):
    allow_parallel: bool


class RequirementClarificationPayload(BaseModel):
    note: str = Field(min_length=3, max_length=2000)


class RequirementAnalysisUpdate(BaseModel):
    title: str = Field(min_length=3, max_length=240)
    user_problem: str = Field(min_length=3, max_length=2000)
    rationality_assessment: str = Field(min_length=3, max_length=2000)
    kano_candidate: str = Field(default="待分析", max_length=20)
    classification_rationale: str = Field(min_length=3, max_length=3000)
    value_hypothesis: str = Field(min_length=3, max_length=1000)
    evidence: str = Field(min_length=3, max_length=3000)
    evidence_gap: str = Field(min_length=3, max_length=2000)
    priority: Literal["P0", "P1", "P2"] = "P1"
    confidence: int = Field(default=0, ge=0, le=100)
    evidence_level: Literal["E0", "E1", "E2", "E3", "E4"] = "E0"


class DocumentRestoreRequest(BaseModel):
    version_id: int = Field(gt=0)


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=4000)
    product_shape: str = Field(default="待确定", max_length=80)
    development_mode: str = Field(default="敏捷迭代", max_length=40)


class ProjectDeleteRequest(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=100)


class ReportingLinesRequest(BaseModel):
    lines: dict[str, str]


class RoleRuntimeConfig(BaseModel):
    provider: Literal["platform", "codex", "claude_code"] = "platform"
    workspace_path: str = Field(default="", max_length=1000)


class TeamConfigRequest(BaseModel):
    member_counts: dict[str, StrictInt] = Field(default_factory=dict)
    role_prompts: dict[str, str] = Field(default_factory=dict)
    role_runtimes: dict[str, RoleRuntimeConfig] = Field(default_factory=dict)


def initial_project_state(project: dict[str, Any]) -> dict[str, Any]:
    description = project["description"].strip()
    all_roles = {rid: 1 for rid in ROLE_INFO}
    documents = [
        ("doc-1", "产品需求包", "产品定义", ["愿景与目标用户", "核心问题与证据", "价值假设与成功指标", "候选需求与 Kano 分类", "待裁决事项"]),
        ("doc-2", "产品需求文档 PRD", "产品定义", ["背景与目标", "范围与非目标", "用户旅程", "功能需求与验收标准", "非功能需求"]),
        ("doc-3", "软件概要设计", "软件设计", ["系统边界", "架构与模块", "Agent 协作拓扑", "数据与记忆方案", "安全和部署"]),
        ("doc-4", "软件详细设计", "软件设计", ["模块职责", "接口与数据结构", "核心流程", "异常处理", "变更记录"]),
        ("doc-5", "测试大纲", "质量保障", ["质量目标", "测试策略", "需求覆盖矩阵", "环境与数据", "退出标准"]),
        ("doc-6", "自测大纲", "质量保障", ["开发自测责任", "单元与组件检查", "冒烟清单", "缺陷分级", "提测门槛"]),
        ("doc-7", "部署与运维方案", "部署运维", ["环境规划", "部署流程", "监控告警", "回滚预案", "运行手册"]),
        ("doc-8", "创始人构想与产品愿景", "产品定义", ["问题与机会", "产品愿景", "核心原则", "创始人原始构想", "待验证假设"]),
        ("doc-9", "决策与待确认事项", "项目治理", ["已确认决策", "待用户确认", "待验证假设", "风险与依赖", "变更记录"]),
    ]
    role_chats = {"group": [], **{role_id: [] for role_id in ROLE_INFO}}
    stamp = datetime.now().astimezone().strftime("%H:%M")
    if description:
        role_chats["pdt"].append({"mine": True, "from": "项目发起人", "who": "我", "time": stamp, "text": description})
    role_chats["pdt"].append({
        "from": ROLE_INFO["pdt"]["name"], "who": "PDT", "color": ROLE_INFO["pdt"]["color"], "time": stamp,
        "text": "项目空间已创建。我会先和你澄清目标用户、真实问题、现有替代方案与成功标准，再整理成候选需求交由你裁决。现在的初始描述只作为待验证输入，不会自动成为需求基线。",
    })
    return {
        "projectId": project["project_id"], "projectName": project["name"],
        "projectDescription": description, "productShape": project["product_shape"],
        "mode": project["development_mode"], "view": "overview", "activeChat": "pdt",
        "cadence": 5, "meetingEnabled": False, "meetingLive": False, "maxParallelTasks": 3,
        "meetingAgenda": ["PDT：需要用户裁决的产品问题", "PM：需求、任务、交付物和依赖进度", "系统工程师：技术决策与变更影响", "各交付角色：下一步、风险与阻塞"],
        "requirements": [], "facts": [], "requirementPlans": {}, "tasks": [], "docs": [
            {"id": doc_id, "name": name, "short": f"{doc_id}.md", "type": doc_type,
             "desc": ("保留项目发起人提供的初始构想；区分原始信息、假设和后续验证结论。" if doc_id == "doc-8" else
                      "记录已确认决策、待用户确认问题和验证假设。" if doc_id == "doc-9" else
                      "项目创建时生成的文档骨架，需求澄清后由对应角色补充。"),
             "content": (f"# 创始人构想与产品愿景\n\n> 初始来源：项目创建信息，尚未验证或批准为需求。\n\n## 初始构想（用户原文）\n\n{description}\n\n## 待澄清\n\n目标用户、问题证据、成功标准和首版范围待 PDT 与项目发起人共同确认。" if doc_id == "doc-8" and description else ""),
             "progress": 5 if doc_id == "doc-8" and description else 0,
             "state": "编写中" if doc_id == "doc-8" and description else "待开始",
             "updated": "刚创建", "owner": "PDT 经理" if doc_id == "doc-8" else "项目经理" if doc_id == "doc-9" else "待分配",
             "version": "v0.1", "sections": sections}
            for doc_id, name, doc_type, sections in documents
        ],
        "meetings": [], "chats": role_chats, "projectDecisions": [], "memberCounts": all_roles,
        "rolePrompts": {}, "roleRuntimes": {},
        "reportingLines": {"pm": "pdt", "se": "pdt", "pe": "pm", "ui": "pm", "fe": "se", "be": "se", "data": "se", "algo": "se", "qa": "pm", "ops": "se"},
        "projectStage": "需求澄清", "goal": "待与用户确认",
        "nonGoals": [], "successMetrics": [], "initialIdea": description,
    }


if store.load("default") is None:
    default_project = store.get_project("default")
    if default_project:
        store.save(initial_project_state(default_project), "default", snapshot_actor="system")
        store.record_event("workspace.initialized", "system", "创建空白本地项目空间与交付物骨架", "default")


def configured_model() -> str:
    return os.getenv("DEEPAGENTS_MODEL", "openai:deepseek-flash").strip()


def runtime_status() -> dict[str, Any]:
    missing: list[str] = []
    if not (os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")):
        missing.append("DEEPSEEK_API_KEY")
    try:
        import deepagents  # noqa: F401
        import langchain_openai  # noqa: F401
    except ImportError:
        missing.append("python dependencies (pip install -r requirements.txt)")
    return {
        "agent_configured": not missing, "model": configured_model(), "missing": missing, "storage": "SQLite",
        "external_runtimes": {
            "codex": {"available": shutil.which("codex") is not None},
            "claude_code": {"available": shutil.which("claude") is not None},
        },
    }


def role_prompt(state: dict[str, Any], role_id: str) -> str:
    custom = state.get("rolePrompts", {}).get(role_id)
    if custom:
        return str(custom)
    info = ROLE_INFO[role_id]
    if role_id == "pdt":
        return (
            "You are the user-facing PDT project manager and the main agent. Make the user's product direction "
            "clear before expanding scope. Treat Kano categories and AI analysis as hypotheses with evidence and "
            "confidence, never as established user truth. Explicitly label analysis to the user as ‘AI建议，非结论’. "
            "The human alone approves requirements for the baseline. "
            "Delegate specialist work to the named subagents, then return a concise recommendation, open question, "
            "and next step. For a proposed requirement, assess whether the problem is specific and worth solving, "
            "separate user-provided facts from assumptions, and use Kano's functional/dysfunctional question logic. "
            "For evidence, use the project's proposed E0-E4 source scale: E0 no source, E1 user statement, E2 project artifact/usage record, "
            "E3 direct interview/observation, E4 repeatable quantitative or independently corroborated evidence. This scale is a project proposal, not a universal standard. "
            "Every draft must include user problem, rationality assessment, value hypothesis, evidence and level, Kano candidate and rationale, confidence, and explicit evidence gaps. "
            "Use DRAFT-* identifiers before approval; only the human approval action assigns the formal REQ-* identifier and baseline version. "
            "If evidence is insufficient, ask a few high-value questions and record the proposal as pending analysis; "
            "if evidence is sufficient, record it for human review and update the product package and PRD. Never "
            "create development, UI, or test tasks until the human approves the requirement. Update affected project "
            "documents when decisions or scope change. Before editing a document, read its current content with "
            "read_project_document, then write the revised full content with update_project_document. Record durable "
            "assumptions and risks through record_project_fact with a traceable source; these remain unverified until "
            "the user reviews them. Read project_facts before relying on remembered context: only ‘用户确认’ is accepted "
            "project context, ‘用户提供’ is a sourced user statement, and ‘待核实’ is an unverified hypothesis/risk. "
            "Use search_project_facts to retrieve older memories by keyword and preserve their source/related IDs. "
            "Keep the user informed."
        )
    return (
        f"You are the {info['name']} for this product project. {info['summary']} Keep your work within your "
        "responsibility. Separate evidence, assumptions, and recommendations. Report progress, next steps, blockers, "
        "and decisions needed from your manager. When you make a meaningful project change, use project tools so it "
        "is recorded. Before editing a document, read it with read_project_document, then write the revised full content "
        "with update_project_document. Record durable assumptions and risks through record_project_fact with a traceable "
        "source; these stay unverified until the user reviews them. Read project_facts before relying on remembered context: "
        "only ‘用户确认’ is accepted project context, ‘用户提供’ is a sourced user statement, and ‘待核实’ is an unverified hypothesis/risk. "
        "Use search_project_facts to retrieve older memories by keyword and preserve their source/related IDs. "
        "Never say a user approved a requirement unless the human approval action is recorded."
    )


def deepagent_imports() -> tuple[Any, Any, Any, Any, Any]:
    from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent, register_harness_profile
    from langchain_core.tools import tool
    return create_deep_agent, register_harness_profile, GeneralPurposeSubagentProfile, HarnessProfile, tool


def safe_agent_profile(model: str) -> None:
    try:
        _, register_profile, general_profile, harness_profile, _ = deepagent_imports()
        register_profile(
            model,
            harness_profile(
                excluded_tools=frozenset({"execute"}),
                general_purpose_subagent=general_profile(enabled=False),
            ),
        )
    except (ImportError, AttributeError, TypeError):
        return


def bump_documents(state: dict[str, Any], doc_ids: list[str], summary: str) -> None:
    for document in state.get("docs", []):
        if document.get("id") not in doc_ids:
            continue
        document["updated"] = "刚刚"
        document["progress"] = min(100, int(document.get("progress", 0)) + 3)
        document["lastChange"] = summary
        try:
            document["version"] = f"v{float(str(document.get('version', 'v0.1'))[1:]) + 0.1:.1f}"
        except (TypeError, ValueError):
            pass


def append_document_record(state: dict[str, Any], doc_ids: list[str], section: str, entry: str) -> None:
    for document in state.get("docs", []):
        if document.get("id") not in doc_ids:
            continue
        content = str(document.get("content") or f"# {document.get('name', '项目文档')}\n")
        if section not in content:
            content = content.rstrip() + f"\n\n{section}\n"
        document["content"] = content.rstrip() + f"\n\n{entry.strip()}\n"
        document["state"] = "编写中"


def next_fact_id(state: dict[str, Any]) -> str:
    ids = [int(match.group(1)) for fact in state.get("facts", []) if (match := re.search(r"FACT-(\d+)", str(fact.get("id", ""))))]
    return f"FACT-{max(ids, default=0) + 1:03d}"


def next_draft_id(state: dict[str, Any]) -> str:
    ids = [int(match.group(1)) for item in state.get("requirements", []) if (match := re.search(r"DRAFT-(\d+)", str(item.get("id", ""))))]
    return f"DRAFT-{max(ids, default=0) + 1:03d}"


def next_baseline_requirement_id(state: dict[str, Any]) -> str:
    ids = [int(match.group(1)) for item in state.get("requirements", []) if (match := re.search(r"REQ-(\d+)", str(item.get("id", ""))))]
    return f"REQ-{max(ids, default=0) + 1:03d}"


def requirement_draft_snapshot(requirement: dict[str, Any]) -> dict[str, Any]:
    return {key: requirement.get(key) for key in (
        "title", "subtitle", "rationality", "kano", "why", "value", "evidence",
        "evidenceLevel", "evidenceGap", "confidence", "priority",
    )}


def append_requirement_decision_history(
    requirement: dict[str, Any], *, action: str, note: str, actor: str, before: dict[str, Any] | None = None,
) -> None:
    history = requirement.setdefault("decisionHistory", [])
    history.append({
        "action": action, "actor": actor, "createdAt": now_iso(), "note": note,
        "before": before, "after": requirement_draft_snapshot(requirement),
        "draftVersion": int(requirement.get("draftVersion", 1)),
        "baselineVersion": requirement.get("baselineVersion"),
    })


def migrate_fact_audit(state: dict[str, Any]) -> bool:
    """Add an explicit baseline version to legacy memory records without inferring provenance."""
    changed = False
    for fact in state.get("facts", []):
        if fact.get("history"):
            continue
        fact.setdefault("version", 1)
        fact.setdefault("evidenceLevel", "E0")
        fact.setdefault("changeSummary", "历史记忆迁移基线；未推断原始证据等级")
        fact.setdefault("reviewedBy", "")
        fact["history"] = [{
            "version": int(fact.get("version", 1)), "actor": fact.get("createdBy", "历史项目记录"),
            "action": "迁移基线", "summary": fact["changeSummary"],
            "status": fact.get("status", "待核实"), "evidenceLevel": fact["evidenceLevel"],
            "confidence": fact.get("confidence", 0), "createdAt": fact.get("createdAt", now_iso()),
        }]
        changed = True
    if changed:
        append_document_record(
            state, ["doc-9"], "## 项目记忆审计迁移",
            "为历史项目记忆补充迁移基线、版本号与来源等级字段；原始等级设为 E0，不推断缺失证据。",
        )
        bump_documents(state, ["doc-9"], "为历史项目记忆补充审计基线")
    return changed


def create_project_fact_record(
    state: dict[str, Any], *, kind: str, title: str, content: str, source: str,
    created_by: str, status: str, confidence: int = 0, requirement_id: str = "",
    task_id: str = "", document_id: str = "", evidence_level: str = "E0",
) -> tuple[dict[str, Any], bool]:
    normalized_title = title.strip()
    normalized_content = content.strip()
    existing = next((
        fact for fact in state.setdefault("facts", [])
        if fact.get("status") != "已取代"
        and fact.get("title", "").strip().casefold() == normalized_title.casefold()
        and fact.get("content", "").strip().casefold() == normalized_content.casefold()
        and fact.get("source", "").strip().casefold() == source.strip().casefold()
        and str(fact.get("requirementId", "")) == requirement_id.strip()
        and str(fact.get("taskId", "")) == task_id.strip()
        and str(fact.get("documentId", "")) == document_id.strip()
    ), None)
    if existing:
        return existing, True
    fact = {
        "id": next_fact_id(state), "kind": kind, "title": normalized_title,
        "content": normalized_content, "source": source.strip(), "status": status,
        "confidence": max(0, min(100, int(confidence))), "createdBy": created_by,
        "evidenceLevel": evidence_level if evidence_level in EVIDENCE_LEVELS else "E0",
        "version": 1, "changeSummary": f"新增项目记忆：{normalized_title}"[:200],
        "reviewedBy": "", "history": [],
        "createdAt": now_iso(), "updatedAt": now_iso(),
        "requirementId": requirement_id.strip(), "taskId": task_id.strip(),
        "documentId": document_id.strip(),
    }
    fact["history"].append({
        "version": 1, "actor": created_by, "action": "创建", "summary": fact["changeSummary"],
        "status": status, "evidenceLevel": fact["evidenceLevel"], "confidence": fact["confidence"],
        "createdAt": fact["createdAt"],
    })
    state["facts"].insert(0, fact)
    summary = f"新增项目记忆 {fact['id']} · {fact['title']}"
    append_document_record(
        state, ["doc-9"], "## 项目事实与假设记录",
        f"### {fact['id']} · {fact['title']}\n\n- 类型：{kind}\n- 状态：{status}\n- 内容：{normalized_content}\n"
        f"- 来源：{source.strip()}\n- 证据等级：{fact['evidenceLevel']}\n- 关联需求：{requirement_id.strip() or '无'}\n"
        f"- 记录人：{created_by}\n- 时间：{fact['createdAt']}",
    )
    bump_documents(state, ["doc-9"], summary)
    return fact, False


def record_requirement_decision(state: dict[str, Any], requirement: dict[str, Any], decision: str, note: str) -> None:
    requirement_id = str(requirement.get("id", ""))
    title = str(requirement.get("title", "")).replace("\n", " ").strip()
    reason = note.strip().replace("\r\n", "\n").replace("\n", "\n  ") or "（未提供）"
    summary = f"用户将 {requirement_id} 裁决为{decision}"
    document_ids = ["doc-1", "doc-2", "doc-9"]
    bump_documents(state, document_ids, summary)
    baseline_label = f"v{requirement.get('baselineVersion')}" if requirement.get("baselineVersion") else "未形成"
    record = (
        f"### {requirement_id} · {decision}\n\n"
        f"- 候选草稿：{requirement.get('draftId') or (requirement_id if requirement_id.startswith('DRAFT-') else '历史草稿编号不可用')}\n"
        f"- 正式需求基线：{baseline_label}\n"
        f"- 需求：{title}\n"
        f"- 裁决时间：{now_iso()}\n"
        f"- 裁决理由：{reason}\n"
    )
    for document in state.get("docs", []):
        if document.get("id") not in document_ids:
            continue
        content = str(document.get("content") or f"# {document.get('name', '项目文档')}\n")
        marker = f"### {requirement_id} · {decision}"
        if marker not in content:
            section = "## 用户裁决记录"
            if section not in content:
                content = content.rstrip() + f"\n\n{section}\n"
            document["content"] = content.rstrip() + f"\n\n{record}"
        document["state"] = "编写中"


TASK_TRANSITIONS: dict[str, set[str]] = {
    "待开始": {"进行中", "有阻塞"},
    "进行中": {"待开始", "评审中", "有阻塞"},
    "评审中": {"进行中", "已完成", "有阻塞"},
    "有阻塞": {"待开始", "进行中"},
    "阻塞中": {"待开始", "进行中"},
    "已完成": {"进行中"},
}


def apply_task_transition(task: dict[str, Any], target: str, note: str, actor: str) -> None:
    current = str(task.get("state", "待开始"))
    if target == current:
        raise HTTPException(status_code=409, detail="任务已经处于该状态")
    if target not in TASK_TRANSITIONS.get(current, set()):
        raise HTTPException(status_code=409, detail=f"不允许任务从“{current}”变更为“{target}”")
    if target in {"已完成", "有阻塞"} and len(note.strip()) < 3:
        label = "完成 / 验收说明" if target == "已完成" else "阻塞原因"
        raise HTTPException(status_code=422, detail=f"请填写至少 3 个字符的{label}")
    if target == "已完成" and len(str(task.get("acceptanceCriteria", "")).strip()) < 3:
        raise HTTPException(status_code=409, detail="任务缺少验收标准，不能标记完成")
    task.setdefault("history", []).append({
        "from": current, "to": target, "note": note.strip(), "actor": actor, "at": now_iso(),
    })
    task["state"] = target
    if target == "已完成":
        task["verificationNote"] = note.strip()
        task["completedAt"] = now_iso()
    if target == "有阻塞":
        task["blocker"] = note.strip()
    elif current in {"有阻塞", "阻塞中"}:
        task.pop("blocker", None)


def enforce_task_parallelism(state: dict[str, Any], task: dict[str, Any]) -> None:
    active = [item for item in state.get("tasks", []) if item.get("state") == "进行中" and item.get("id") != task.get("id")]
    limit = max(1, min(32, int(state.get("maxParallelTasks", 3) or 3)))
    if active and (task.get("parallelAllowed", True) is False or any(item.get("parallelAllowed", True) is False for item in active)):
        raise HTTPException(status_code=409, detail="该任务设置为串行，或当前已有串行任务；请先完成/暂停其他进行中任务")
    if len(active) >= limit:
        raise HTTPException(status_code=409, detail=f"当前并行上限为 {limit}，已有 {len(active)} 项进行中；请提高上限或释放并行名额")


def record_task_change(state: dict[str, Any], task: dict[str, Any], summary: str) -> None:
    doc_ids = ["doc-4", "doc-5", "doc-6", "doc-9"]
    bump_documents(state, doc_ids, summary)
    entry = (
        f"### {task.get('id')} · {task.get('title', '任务变更')}\n\n"
        f"- 需求：{task.get('req', '未关联')}\n"
        f"- 状态：{task.get('state', '待开始')}\n"
        f"- 负责人：{task.get('owner', '待分配')}\n"
        f"- 验收标准：{task.get('acceptanceCriteria', '待补充')}\n"
    )
    if task.get("verificationNote"):
        entry += f"- 验收记录：{task['verificationNote']}\n"
    if task.get("blocker"):
        entry += f"- 当前阻塞：{task['blocker']}\n"
    entry += f"- 更新时间：{now_iso()}\n"
    for document in state.get("docs", []):
        if document.get("id") not in doc_ids:
            continue
        content = str(document.get("content") or f"# {document.get('name', '项目文档')}\n")
        section = "## 任务状态与验收记录"
        if section not in content:
            content = content.rstrip() + f"\n\n{section}\n"
        document["content"] = content.rstrip() + f"\n\n{entry}"
        document["state"] = "编写中"


def next_task_id(state: dict[str, Any]) -> str:
    task_numbers = []
    for task in state.get("tasks", []):
        match = re.search(r"(\d+)", str(task.get("id", "")))
        if match:
            task_numbers.append(int(match.group(1)))
    greatest = max(task_numbers, default=0)
    return f"TASK-{greatest + 1:03d}"


def next_meeting_id(state: dict[str, Any]) -> str:
    numbers = [int(match.group(1)) for item in state.get("meetings", []) if (match := re.search(r"MEET-(\d+)", str(item.get("id", ""))))]
    return f"MEET-{max(numbers, default=0) + 1:03d}"


def parse_meeting_minutes(raw: str, state: dict[str, Any]) -> dict[str, Any]:
    """Parse PM JSON minutes, keeping unstructured output readable and non-actionable."""
    candidate = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    try:
        payload = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return {"summary": raw.strip(), "decisionRequests": [], "deliveryOrder": [], "actionItems": []}
    if not isinstance(payload, dict):
        return {"summary": raw.strip(), "decisionRequests": [], "deliveryOrder": [], "actionItems": []}

    approved = {str(item.get("id")) for item in state.get("requirements", []) if item.get("state") == "已确认"}
    role_aliases = {role_id: role_id for role_id in ROLE_INFO}
    for role_id, info in ROLE_INFO.items():
        role_aliases[info["short"].lower()] = role_id
        role_aliases[info["name"]] = role_id
    raw_actions = payload.get("actionItems", payload.get("actions", []))
    action_items: list[dict[str, Any]] = []
    if isinstance(raw_actions, list):
        for index, raw_action in enumerate(raw_actions[:12], start=1):
            if not isinstance(raw_action, dict):
                continue
            title = str(raw_action.get("title", "")).strip()[:240]
            role_value = str(raw_action.get("assigneeRole", raw_action.get("ownerRole", raw_action.get("role", "")))).strip()
            role_id = role_aliases.get(role_value, role_aliases.get(role_value.lower(), ""))
            requirement_id = str(raw_action.get("requirementId", raw_action.get("requirement_id", ""))).strip()
            criteria = str(raw_action.get("acceptanceCriteria", raw_action.get("acceptance_criteria", ""))).strip()[:4000]
            action_items.append({
                "id": f"ACT-{index:03d}", "title": title, "assigneeRole": role_id,
                "requirementId": requirement_id, "acceptanceCriteria": criteria,
                "due": str(raw_action.get("due", "待排期")).strip()[:40] or "待排期",
                "source": str(raw_action.get("source", "会议 AI 建议，待项目经理确认")).strip()[:500],
                "status": "待确认", "taskId": None,
                "eligible": bool(title and role_id in ROLE_INFO and role_id != "pdt" and requirement_id in approved and len(criteria) >= 3),
                "aiSuggested": True,
            })
    return {
        "summary": str(payload.get("summary", "")).strip() or raw.strip(),
        "decisionRequests": [str(item).strip()[:1000] for item in payload.get("decisionRequests", []) if str(item).strip()][:12] if isinstance(payload.get("decisionRequests", []), list) else [],
        "deliveryOrder": [str(item).strip()[:500] for item in payload.get("deliveryOrder", []) if str(item).strip()][:12] if isinstance(payload.get("deliveryOrder", []), list) else [],
        "actionItems": action_items,
    }


def project_tools(project_id: str, role_id: str) -> list[Any]:
    _, _, _, _, tool = deepagent_imports()

    @tool
    def read_project_context() -> dict[str, Any]:
        """Read approved direction, open requirements, assigned tasks, and document status."""
        state = store.load(project_id) or {}
        return {
            "project_name": state.get("projectName", ""),
            "project_description": state.get("projectDescription", ""),
            "product_shape": state.get("productShape", "待确定"),
            "project_goal": state.get("goal", "待与用户确认"),
            "project_stage": state.get("projectStage", "需求澄清"),
            "project_mode": state.get("mode", "敏捷迭代"),
            "data_caveats": ([
                "This is the inherited default prototype workspace. Historical requirements, chats, meeting statements, and research claims may be illustrative or unverified. Do not treat them as customer evidence or approved scope unless their source and decision are explicitly verified with the project owner."
            ] if project_id == "default" and state.get("requirements") else []),
            "reporting_lines": state.get("reportingLines", {}),
            "requirements": [
                {k: r.get(k) for k in ("id", "draftId", "draftVersion", "baselineVersion", "title", "kano", "confidence", "evidenceLevel", "evidence", "evidenceGap", "rationality", "state", "priority", "owner", "why", "value", "aiRecommendationLabel")}
                for r in state.get("requirements", [])
            ],
            "project_facts": [
                {
                    **{k: fact.get(k) for k in ("id", "kind", "title", "status", "confidence", "evidenceLevel", "version", "requirementId", "taskId", "documentId")},
                    "content": str(fact.get("content", ""))[:1200], "source": str(fact.get("source", ""))[:400],
                }
                for fact in [item for item in state.get("facts", []) if item.get("status") != "已取代"][-30:]
            ],
            "tasks": [
                {k: t.get(k) for k in ("id", "title", "req", "state", "owner", "due")}
                for t in state.get("tasks", [])
            ],
            "documents": [
                {k: d.get(k) for k in ("id", "name", "version", "state", "owner", "progress", "lastChange")}
                for d in state.get("docs", [])
            ],
            "recent_decisions": state.get("projectDecisions", [])[-8:],
        }

    @tool
    def search_project_facts(query: str, limit: int = 8) -> dict[str, Any]:
        """Find older project memories by keyword and return their provenance and review status."""
        normalized = query.strip().casefold()
        if len(normalized) < 2:
            return {"error": "Search query must contain at least two characters."}
        terms = [term for term in re.split(r"\s+", normalized) if term][:8]
        state = store.load(project_id) or {}
        matches = []
        status_priority = {"用户确认": 3, "用户提供": 2, "待核实": 1}
        for fact in state.get("facts", []):
            if fact.get("status") == "已取代":
                continue
            fields = [
                str(fact.get("title", "")).casefold(), str(fact.get("content", "")).casefold(),
                str(fact.get("source", "")).casefold(), str(fact.get("kind", "")).casefold(),
                str(fact.get("requirementId", "")).casefold(), str(fact.get("taskId", "")).casefold(),
                str(fact.get("documentId", "")).casefold(),
            ]
            score = sum(2 if term in fields[0] else 1 if any(term in field for field in fields[1:]) else 0 for term in terms)
            if score:
                matches.append((score, status_priority.get(str(fact.get("status")), 0), fact))
        matches.sort(key=lambda item: (item[0], item[1], str(item[2].get("createdAt", ""))), reverse=True)
        capped_limit = max(1, min(int(limit), 12))
        return {
            "query": query,
            "matches": [
                {
                    **{key: fact.get(key) for key in (
                        "id", "kind", "title", "status", "confidence", "evidenceLevel", "version",
                        "changeSummary", "createdBy", "createdAt", "updatedAt", "requirementId", "taskId", "documentId",
                    )},
                    "content": str(fact.get("content", ""))[:1600], "source": str(fact.get("source", ""))[:500],
                }
                for _, _, fact in matches[:capped_limit]
            ],
            "total_matches": len(matches),
            "status_legend": "用户确认 = accepted project context; 用户提供 = sourced statement from the project owner; 待核实 = candidate only, not a fact.",
        }

    @tool
    @synchronized
    def record_project_fact(
        kind: str, title: str, content: str, source: str, confidence: int = 0,
        requirement_id: str = "", task_id: str = "", document_id: str = "", evidence_level: str = "E0",
    ) -> dict[str, Any]:
        """Save a project assumption or risk for later reuse. AI entries stay unverified until the user reviews them."""
        if kind not in {"假设", "风险"}:
            return {"error": "AI may only suggest an assumption or risk; only the human may record a fact, evidence, or decision."}
        if len(title.strip()) < 3 or len(content.strip()) < 3 or len(source.strip()) < 3:
            return {"error": "A project memory entry needs a title, content, and traceable source (at least 3 characters each)."}
        state = store.load(project_id) or {}
        related_objects = {
            "requirement_id": (requirement_id, {str(item.get("id")) for item in state.get("requirements", [])}),
            "task_id": (task_id, {str(item.get("id")) for item in state.get("tasks", [])}),
            "document_id": (document_id, {str(item.get("id")) for item in state.get("docs", [])}),
        }
        for label, (value, valid_ids) in related_objects.items():
            if value.strip() and value.strip() not in valid_ids:
                return {"error": f"Related project object does not exist: {label}={value.strip()}"}
        fact, duplicate = create_project_fact_record(
            state, kind=kind, title=title, content=content, source=source,
            created_by=ROLE_INFO[role_id]["name"], status="待核实", confidence=confidence,
            requirement_id=requirement_id, task_id=task_id, document_id=document_id,
            evidence_level=evidence_level if evidence_level in EVIDENCE_LEVELS else "E0",
        )
        if not duplicate:
            append_chat(state, "group", {
                "from": ROLE_INFO[role_id]["name"], "who": ROLE_INFO[role_id]["short"],
                "color": ROLE_INFO[role_id]["color"], "time": datetime.now().astimezone().strftime("%H:%M"),
                "text": f"已记录待核实项目记忆 {fact['id']}「{fact['title']}」；来源：{fact['source']}",
            })
            store.save(state, project_id, snapshot_actor=ROLE_INFO[role_id]["name"])
            store.record_event("fact.proposed", ROLE_INFO[role_id]["name"], f"记录待核实 {fact['id']}：{fact['title']}", project_id)
        return {"fact": fact, "duplicate": duplicate, "human_review_required": True}

    @tool
    def read_project_document(document_id: str) -> dict[str, Any]:
        """Read one project's document content and metadata before revising it."""
        state = store.load(project_id) or {}
        document = next((d for d in state.get("docs", []) if d.get("id") == document_id), None)
        if not document:
            return {"error": f"Document {document_id} does not exist"}
        return {k: document.get(k) for k in ("id", "name", "type", "desc", "version", "state", "owner", "progress", "sections", "content", "lastChange")}

    @tool
    @synchronized
    def create_requirement(
        title: str,
        user_problem: str,
        rationality_assessment: str,
        kano_candidate: str,
        classification_rationale: str,
        value_hypothesis: str,
        evidence: str,
        evidence_gap: str,
        priority: str = "P1",
        confidence: int = 0,
        evidence_level: str = "E0",
    ) -> dict[str, Any]:
        """Add a DRAFT-* candidate with rationality, value, evidence source level, Kano rationale, confidence, and gaps. Only human approval assigns a formal REQ-* baseline identity."""
        state = store.load(project_id) or {}
        required_fields = {
            "标题": title, "用户问题": user_problem, "合理性评估": rationality_assessment,
            "Kano 判断依据": classification_rationale, "价值假设": value_hypothesis,
            "当前证据": evidence, "证据缺口": evidence_gap,
        }
        missing = [label for label, value in required_fields.items() if len(str(value).strip()) < 3]
        if missing:
            return {"error": "候选需求必填字段不足：" + "、".join(missing), "human_approval_required": True}
        requirements = state.setdefault("requirements", [])
        req_id = next_draft_id(state)
        valid_kano = {"魅力型", "期望型", "基本型", "无差异型", "反向型", "待分析"}
        normalized_kano = kano_candidate if kano_candidate in valid_kano else "待分析"
        normalized_confidence = max(0, min(100, int(confidence))) if normalized_kano != "待分析" else 0
        normalized_evidence_level = evidence_level if evidence_level in EVIDENCE_LEVELS else "E0"
        record = {
            "id": req_id, "title": title, "subtitle": user_problem,
            "draftVersion": 1, "createdAt": now_iso(), "createdBy": ROLE_INFO[role_id]["name"],
            "aiRecommendationLabel": "AI建议，非结论", "rationality": rationality_assessment,
            "evidenceGap": evidence_gap, "decisionHistory": [],
            "kano": normalized_kano,
            "confidence": normalized_confidence,
            "evidenceLevel": normalized_evidence_level,
            "priority": priority if priority in {"P0", "P1", "P2"} else "P1",
            "state": "分析中" if normalized_kano == "待分析" else "待你裁决",
            "owner": ROLE_INFO["pe"]["name"],
            "why": classification_rationale, "value": value_hypothesis,
            "evidence": evidence, "effort": "待角色估算", "tests": 0,
        }
        requirements.insert(0, record)
        change = f"新增候选需求 {req_id}，等待用户裁决"
        bump_documents(state, ["doc-1", "doc-2"], change)
        append_document_record(
            state, ["doc-1", "doc-2"], "## 候选需求分析记录",
            f"### {req_id} · {title}\n\n- Kano 建议：{normalized_kano}（信心度 {normalized_confidence}%）\n"
            f"- 用户问题：{user_problem}\n- 合理性评估：{rationality_assessment}\n- Kano 判断依据：{classification_rationale}\n"
            f"- 价值假设：{value_hypothesis}\n- 证据等级：{normalized_evidence_level}\n- 证据与来源：{evidence}\n"
            f"- 证据缺口：{evidence_gap}\n- AI 分析标识：AI建议，非结论\n- 状态：{record['state']}\n- 时间：{now_iso()}",
        )
        store.save(state, project_id, snapshot_actor=ROLE_INFO[role_id]["name"])
        store.record_event("requirement.proposed", ROLE_INFO[role_id]["name"], f"新增 {req_id}: {title}", project_id)
        return {"requirement": record, "human_approval_required": True}

    @tool
    @synchronized
    def create_development_task(
        requirement_id: str, title: str, assignee_role: str, acceptance_criteria: str
    ) -> dict[str, Any]:
        """Create a development or test task linked to a requirement, role, and acceptance criteria."""
        if assignee_role not in ROLE_INFO:
            return {"error": "Unknown role id", "valid_role_ids": list(ROLE_INFO)}
        state = store.load(project_id) or {}
        requirement = next((r for r in state.get("requirements", []) if r.get("id") == requirement_id), None)
        if requirement is None:
            return {"error": f"Requirement {requirement_id} does not exist"}
        if requirement.get("state") != "已确认":
            return {"error": f"Requirement {requirement_id} is not approved by the human; no work task was created"}
        if len(acceptance_criteria.strip()) < 3:
            return {"error": "A task needs verifiable acceptance criteria"}
        info = ROLE_INFO[assignee_role]
        active_plan = state.get("requirementPlans", {}).get(requirement_id, {})
        planning_attempt = int(active_plan.get("attempts", 0)) if active_plan.get("status") == "running" else 0
        normalized_title = title.strip().casefold()
        if planning_attempt:
            duplicate = next((
                task for task in state.get("tasks", [])
                if task.get("req") == requirement_id and task.get("who") == info["short"]
                and int(task.get("planAttempt", 0)) == planning_attempt
                and str(task.get("title", "")).strip().casefold() == normalized_title
            ), None)
            if duplicate:
                return {**duplicate, "duplicate": True}
        task = {
            "id": next_task_id(state), "title": title, "req": requirement_id,
            "state": "待开始", "owner": info["name"], "who": info["short"],
            "color": info["color"], "due": "待排期", "acceptanceCriteria": acceptance_criteria,
            "parallelAllowed": True,
        }
        if planning_attempt:
            task["planAttempt"] = planning_attempt
        state.setdefault("tasks", []).insert(0, task)
        record_task_change(state, task, f"新增 {task['id']} · {title}")
        append_chat(state, "group", {
            "from": ROLE_INFO[role_id]["name"], "who": ROLE_INFO[role_id]["short"],
            "color": ROLE_INFO[role_id]["color"], "time": datetime.now().astimezone().strftime("%H:%M"),
            "text": f"已创建 {task['id']}「{title}」，关联 {requirement_id}，负责人：{info['name']}。验收标准：{acceptance_criteria}",
        })
        store.save(state, project_id)
        store.record_event("task.created", ROLE_INFO[role_id]["name"], f"创建 {task['id']} · {title}", project_id)
        return task

    @tool
    @synchronized
    def update_development_task(task_id: str, target_state: str, note: str) -> dict[str, Any]:
        """Move an assigned task through delivery, review, acceptance, or blocker states with a written note."""
        state = store.load(project_id) or {}
        task = next((t for t in state.get("tasks", []) if t.get("id") == task_id), None)
        if task is None:
            return {"error": f"Task {task_id} does not exist"}
        short = ROLE_INFO[role_id]["short"]
        if role_id not in {"pdt", "pm", "se"} and task.get("who") != short:
            return {"error": "Only the assigned role or a project manager can update this task"}
        previous = task.get("state", "待开始")
        try:
            if target_state == "进行中":
                enforce_task_parallelism(state, task)
            apply_task_transition(task, target_state, note, ROLE_INFO[role_id]["name"])
        except HTTPException as error:
            return {"error": error.detail}
        summary = f"{task_id}：{previous} → {target_state}"
        record_task_change(state, task, summary)
        append_chat(state, "group", {
            "from": ROLE_INFO[role_id]["name"], "who": short, "color": ROLE_INFO[role_id]["color"],
            "time": datetime.now().astimezone().strftime("%H:%M"), "text": f"{summary}。{note.strip()}",
        })
        store.save(state, project_id, snapshot_actor=ROLE_INFO[role_id]["name"])
        store.record_event("task.status_changed", ROLE_INFO[role_id]["name"], f"{summary} · {note[:140]}", project_id)
        return {"task": task, "state": state}

    @tool
    @synchronized
    def update_requirement_analysis(
        requirement_id: str, title: str, user_problem: str, rationality_assessment: str,
        kano_candidate: str, classification_rationale: str, value_hypothesis: str,
        evidence: str, evidence_gap: str,
        priority: str = "P1", confidence: int = 0, evidence_level: str = "E0",
    ) -> dict[str, Any]:
        """Update a requirement after clarification and return it to the human decision queue."""
        if role_id not in {"pdt", "pe"}:
            return {"error": "Only PDT or product engineering can revise requirement analysis"}
        state = store.load(project_id) or {}
        requirement = next((r for r in state.get("requirements", []) if r.get("id") == requirement_id), None)
        if requirement is None:
            return {"error": f"Requirement {requirement_id} does not exist"}
        if requirement.get("state") not in {"分析中", "待你裁决", "挂起待证据"}:
            return {"error": "Only an in-analysis or pending requirement can be revised"}
        previous = {key: requirement.get(key) for key in ("title", "subtitle", "rationality", "kano", "why", "value", "evidence", "evidenceGap", "evidenceLevel", "priority", "confidence")}
        valid_kano = {"魅力型", "期望型", "基本型", "无差异型", "反向型", "待分析"}
        normalized_kano = kano_candidate if kano_candidate in valid_kano else "待分析"
        normalized_evidence_level = evidence_level if evidence_level in EVIDENCE_LEVELS else "E0"
        requirement.update({
            "title": title.strip(), "subtitle": user_problem.strip(), "kano": normalized_kano,
            "rationality": rationality_assessment.strip(), "evidenceGap": evidence_gap.strip(),
            "aiRecommendationLabel": "AI建议，非结论",
            "confidence": max(0, min(100, int(confidence))) if normalized_kano != "待分析" else 0,
            "evidenceLevel": normalized_evidence_level,
            "priority": priority if priority in {"P0", "P1", "P2"} else "P1",
            "state": "待你裁决", "owner": ROLE_INFO["pe"]["name"],
            "why": classification_rationale.strip(), "value": value_hypothesis.strip(),
            "evidence": evidence.strip(), "analysisUpdatedAt": now_iso(),
            "analysisHistory": requirement.get("analysisHistory", []) + [{"previous": previous, "updatedAt": now_iso()}],
        })
        bump_documents(state, ["doc-1", "doc-2"], f"澄清并更新 {requirement_id}，重新提交用户裁决")
        append_document_record(
            state, ["doc-1", "doc-2"], "## 候选需求分析记录",
            f"### {requirement_id} · 补充分析 v{len(requirement.get('analysisHistory', []))}\n\n"
            f"- 需求：{title.strip()}\n- Kano 建议：{normalized_kano}（信心度 {requirement['confidence']}%）\n"
            f"- 用户问题：{user_problem.strip()}\n- 合理性评估：{rationality_assessment.strip()}\n- Kano 判断依据：{classification_rationale.strip()}\n"
            f"- 价值假设：{value_hypothesis.strip()}\n- 证据等级：{normalized_evidence_level}\n- 证据：{evidence.strip()}\n"
            f"- 证据缺口：{evidence_gap.strip()}\n- AI 分析标识：AI建议，非结论\n- 状态：待你裁决\n- 时间：{now_iso()}",
        )
        append_chat(state, "group", {
            "from": ROLE_INFO[role_id]["name"], "who": ROLE_INFO[role_id]["short"],
            "color": ROLE_INFO[role_id]["color"], "time": datetime.now().astimezone().strftime("%H:%M"),
            "text": f"{requirement_id} 的补充分析已完成，重新提交用户裁决；分类：{normalized_kano}（{requirement['confidence']}% 信心）。",
        })
        store.save(state, project_id, snapshot_actor=ROLE_INFO[role_id]["name"])
        store.record_event("requirement.analysis_updated", ROLE_INFO[role_id]["name"], f"重新提交 {requirement_id} 裁决", project_id)
        return {"requirement": requirement}

    @tool
    @synchronized
    def update_project_document(document_id: str, change_summary: str, content: str) -> dict[str, Any]:
        """Write a project deliverable change and advance its review version."""
        state = store.load(project_id) or {}
        document = next((d for d in state.get("docs", []) if d.get("id") == document_id), None)
        if not document:
            return {"error": f"Document {document_id} does not exist"}
        document["content"] = content[:24000]
        document["lastChange"] = change_summary
        document["updated"] = "刚刚"
        document["state"] = "评审中"
        try:
            document["version"] = f"v{float(str(document.get('version', 'v0.1'))[1:]) + 0.1:.1f}"
        except (TypeError, ValueError):
            document["version"] = "v0.2"
        store.save(state, project_id, snapshot_actor=ROLE_INFO[role_id]["name"])
        store.record_event("document.updated", ROLE_INFO[role_id]["name"], f"更新 {document.get('name')}: {change_summary}", project_id)
        return {"id": document_id, "version": document["version"], "state": document["state"]}

    @tool
    @synchronized
    def propose_user_decision(title: str, proposal: str, rationale: str, requirement_id: str = "") -> dict[str, Any]:
        """Record a recommendation for PDT and the user; this tool does not apply or approve it."""
        state = store.load(project_id) or {}
        if requirement_id and not any(requirement_id in {str(item.get("id", "")), str(item.get("draftId", ""))} for item in state.get("requirements", [])):
            return {"error": f"Requirement {requirement_id} does not exist"}
        decision = {
            "id": f"DEC-{len(state.get('projectDecisions', [])) + 1:03d}",
            "title": title, "proposal": proposal, "rationale": rationale,
            "requirement": requirement_id, "state": "待 PDT 与用户确认", "createdAt": now_iso(),
            "proposedBy": ROLE_INFO[role_id]["name"], "version": 1,
            "history": [{"version": 1, "action": "提出建议", "actor": ROLE_INFO[role_id]["name"], "state": "待 PDT 与用户确认", "summary": "AI 提交待用户审阅的建议", "createdAt": now_iso()}],
        }
        state.setdefault("projectDecisions", []).append(decision)
        append_document_record(
            state, ["doc-9"], "## 待用户审阅的决策建议",
            f"### {decision['id']} · {title}\n\n- 建议：{proposal}\n- 依据：{rationale}\n- 关联需求：{requirement_id or '无'}\n- 提议角色：{ROLE_INFO[role_id]['name']}\n- 状态：待 PDT 与用户确认\n- 时间：{decision['createdAt']}",
        )
        append_chat(state, "group", {
            "from": ROLE_INFO[role_id]["name"], "who": ROLE_INFO[role_id]["short"], "color": ROLE_INFO[role_id]["color"],
            "time": datetime.now().astimezone().strftime("%H:%M"),
            "text": f"{decision['id']}「{title}」已提交用户审阅：{proposal}。依据：{rationale}",
        })
        store.save(state, project_id, snapshot_actor=ROLE_INFO[role_id]["name"])
        store.record_event("decision.proposed", ROLE_INFO[role_id]["name"], title, project_id)
        return decision

    @tool
    @synchronized
    def send_project_update(message: str) -> dict[str, str]:
        """Post an update to the project group for related managers and collaborators."""
        state = store.load(project_id) or {}
        info = ROLE_INFO[role_id]
        state.setdefault("chats", {}).setdefault("group", []).append({
            "from": info["name"], "who": info["short"], "color": info["color"],
            "time": datetime.now().astimezone().strftime("%H:%M"), "text": message[:4000],
        })
        store.save(state, project_id)
        store.record_event("message.posted", info["name"], message[:180], project_id)
        return {"status": "posted", "audience": "项目群及相关上级"}

    @tool
    async def delegate_to_role(target_role: str, assignment: str) -> dict[str, str]:
        """Delegate a focused assignment to a project role using that role's configured runtime, then sync its report to the role chat and project group."""
        if target_role not in ROLE_INFO or target_role == "pdt":
            return {"error": "Choose a valid specialist role other than PDT."}
        if len(assignment.strip()) < 5:
            return {"error": "Provide a clear assignment of at least five characters."}
        target_info = ROLE_INFO[target_role]
        store.record_event("agent.delegation_started", ROLE_INFO[role_id]["name"], f"委派 {target_info['name']}：{assignment[:120]}", project_id)
        answer = await invoke_role(project_id, target_role, assignment.strip())
        state = store.load(project_id) or {}
        stamp = datetime.now().astimezone().strftime("%H:%M")
        append_chat(state, target_role, {
            "from": target_info["name"], "who": target_info["short"], "color": target_info["color"],
            "time": stamp, "text": answer,
        })
        append_chat(state, "group", {
            "from": target_info["name"], "who": target_info["short"], "color": target_info["color"],
            "time": stamp, "text": f"受 {ROLE_INFO[role_id]['name']} 委派的工作已完成：\n\n{answer[:3500]}",
        })
        store.save(state, project_id, snapshot_actor=target_info["name"])
        store.record_event("agent.delegation_completed", target_info["name"], f"完成 {ROLE_INFO[role_id]['name']} 委派任务", project_id)
        return {"role": target_info["name"], "report": answer}

    tool_map = {
        "context": read_project_context, "knowledge": record_project_fact, "knowledge_search": search_project_facts, "requirement": create_requirement,
        "task": create_development_task, "task_update": update_development_task,
        "requirement_update": update_requirement_analysis, "read_document": read_project_document,
        "document": update_project_document,
        "decision": propose_user_decision, "message": send_project_update,
    }
    allowed = {
        "pdt": ("context", "knowledge", "knowledge_search", "requirement", "requirement_update", "task", "task_update", "read_document", "document", "decision", "message"),
        "pm": ("context", "knowledge", "knowledge_search", "task", "task_update", "read_document", "document", "decision", "message"),
        "se": ("context", "knowledge", "knowledge_search", "task", "task_update", "read_document", "document", "decision", "message"),
        "pe": ("context", "knowledge", "knowledge_search", "requirement", "requirement_update", "read_document", "document", "decision", "message"),
        "qa": ("context", "knowledge", "knowledge_search", "task", "task_update", "read_document", "document", "message"),
        "ops": ("context", "knowledge", "knowledge_search", "task", "task_update", "read_document", "document", "message"),
    }
    result = [tool_map[name] for name in allowed.get(role_id, ("context", "knowledge", "knowledge_search", "task", "task_update", "read_document", "document", "message"))]
    if role_id == "pdt":
        result.append(delegate_to_role)
    return result


def build_agent(project_id: str, role_id: str) -> Any:
    if role_id not in ROLE_INFO:
        role_id = "pdt"
    state = store.load(project_id) or {}
    model = configured_model()
    prompt = state.get("rolePrompts", {}).get(role_id) or role_prompt(state, role_id)
    role_runtimes = state.get("roleRuntimes", {})
    external_roles = [rid for rid, config in role_runtimes.items() if isinstance(config, dict) and config.get("provider", "platform") != "platform"]
    if role_id == "pdt" and external_roles:
        names = "、".join(ROLE_INFO[rid]["name"] for rid in external_roles if rid in ROLE_INFO and rid != "pdt")
        if names:
            prompt += f"\n\n外部子 Runtime 路由：{names} 当前配置为 Codex 或 Claude Code 子 Runtime。不要把这些角色当作 Deep Agents 内置子 Agent 调用；请使用 delegate_to_role 工具委派，工具会按各自配置运行并把报告同步到项目群。"
    prototype_notice = ""
    if project_id == "default" and state.get("requirements"):
        prototype_notice = (
            "\n\nPROJECT DATA QUALITY NOTICE: This inherited default workspace contains prototype-era historical requirements, chats, and meeting statements. "
            "Treat their research claims, approval status, and decisions as unverified unless the project owner confirms their source. "
            "Do not cite them as customer evidence or as approved product scope. Prefer explicit owner confirmation and current project documents."
        )
        prompt += prototype_notice
    role_config = json.dumps(
        {"memberCounts": state.get("memberCounts", {}), "rolePrompts": state.get("rolePrompts", {}), "roleRuntimes": role_runtimes}
        if role_id == "pdt" else state.get("rolePrompts", {}).get(role_id, ""),
        ensure_ascii=False, sort_keys=True,
    )
    key = (project_id, role_id, model + prompt, role_config)
    if key in AGENT_CACHE:
        return AGENT_CACHE[key]
    for old_key in [candidate for candidate in AGENT_CACHE if candidate[:2] == (project_id, role_id) and candidate != key]:
        AGENT_CACHE.pop(old_key, None)
    create_deep_agent, _, _, _, _ = deepagent_imports()
    safe_agent_profile(model)
    args: dict[str, Any] = {
        "model": model, "tools": project_tools(project_id, role_id), "system_prompt": prompt,
    }
    if role_id == "pdt":
        subagents = []
        for rid in ROLE_ORDER:
            count = max(1, min(12, int(state.get("memberCounts", {}).get(rid, 1))))
            runtime_config = role_runtimes.get(rid, {})
            if isinstance(runtime_config, dict) and runtime_config.get("provider", "platform") != "platform":
                continue
            for member_number in range(1, count + 1):
                subagents.append({
                    "name": rid if member_number == 1 else f"{rid}-{member_number}",
                    "description": f"{ROLE_INFO[rid]['name']} {member_number} — {ROLE_INFO[rid]['summary']}",
                    "model": model,
                    "system_prompt": (state.get("rolePrompts", {}).get(rid) or role_prompt(state, rid))
                    + prototype_notice
                    + (f" You are specialist {member_number} for this role; coordinate through project tasks." if member_number > 1 else ""),
                    "tools": project_tools(project_id, rid),
                })
        args["subagents"] = subagents
    agent = create_deep_agent(**args)
    AGENT_CACHE[key] = agent
    return agent


def as_chat_role(room: str) -> str:
    return "fe" if room == "dev" else room if room in ROLE_INFO else "pdt"


def message_content(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("text"))
    return str(content)


def external_message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(external_message_text(item) for item in value if item is not None).strip()
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if "content" in value:
            return external_message_text(value["content"])
    return ""


async def invoke_external_runtime(
    project_id: str, role_id: str, prompt: str, history: list[dict[str, str]] | None = None,
) -> str:
    state = store.load(project_id) or {}
    config = (state.get("roleRuntimes") or {}).get(role_id) or {}
    provider = str(config.get("provider", "platform"))
    if provider not in {"codex", "claude_code"}:
        raise RuntimeError("未知的角色 Runtime 配置")
    command_name = "codex" if provider == "codex" else "claude"
    executable = shutil.which(command_name)
    if not executable:
        label = "Codex CLI" if provider == "codex" else "Claude Code CLI"
        raise RuntimeError(f"当前服务环境没有找到 {label}。请先安装并登录该 CLI，再重试。")
    raw_workspace = str(config.get("workspacePath", "")).strip()
    if not raw_workspace:
        raise RuntimeError("该角色的子 Runtime 尚未配置代码工作区目录。")
    workspace = Path(raw_workspace).expanduser()
    if not workspace.is_absolute():
        raise RuntimeError("子 Runtime 工作区必须使用绝对路径。")
    try:
        workspace = workspace.resolve(strict=True)
    except OSError as error:
        raise RuntimeError("子 Runtime 工作区不存在或不可访问，请检查目录路径。") from error
    if not workspace.is_dir():
        raise RuntimeError("子 Runtime 工作区路径必须指向一个目录。")

    info = ROLE_INFO[role_id]
    recent_history = history if history is not None else state_messages_for_chat(state, role_id, prompt)
    conversation = "\n\n".join(
        f"{'用户' if item.get('role') == 'user' else info['name']}：{str(item.get('content', ''))[:3000]}"
        for item in recent_history[-12:]
    )
    context = {
        "project": {
            "name": state.get("projectName", ""), "description": state.get("projectDescription", ""),
            "product_shape": state.get("productShape", "待确定"), "mode": state.get("mode", "敏捷迭代"),
            "goal": state.get("goal", "待与用户确认"), "stage": state.get("projectStage", "需求澄清"),
        },
        "requirements": [
            {key: item.get(key) for key in ("id", "title", "state", "kano", "priority", "rationality", "value", "evidenceGap")}
            for item in state.get("requirements", [])[-30:]
        ],
        "tasks": [
            {key: item.get(key) for key in ("id", "title", "req", "state", "owner", "acceptanceCriteria")}
            for item in state.get("tasks", [])[-40:]
        ],
        "documents": [
            {key: item.get(key) for key in ("id", "name", "version", "state", "owner", "lastChange")}
            for item in state.get("docs", [])
        ],
        "open_decisions": [
            {key: item.get(key) for key in ("id", "title", "proposal", "rationale", "state")}
            for item in state.get("projectDecisions", [])[-15:]
        ],
    }
    configured_prompt = state.get("rolePrompts", {}).get(role_id) or role_prompt(state, role_id)
    complete_prompt = (
        f"你是 DevTing 项目中的{info['name']}，职责：{info['summary']}\n\n"
        f"角色要求：\n{configured_prompt}\n\n"
        "当前 DevTing 项目上下文（用于参考）：\n"
        f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))[:24000]}\n\n"
        "重要集成边界：你正在独立的本地代码子 Runtime 中运行。你可以检查或修改所选工作区内的代码；"
        "你没有 DevTing SQLite 项目工具，因此不要声称已在 DevTing 更新任务、需求、项目文档或决策。"
        "如需这些结构化记录变更，请在报告中提出，由平台 Runtime/PDT/PM 使用项目工具完成。"
        "遇到高影响范围或方向决策时，明确标记为需要用户确认，不要代替用户批准。\n\n"
        f"最近角色会话：\n{conversation}\n\n"
        f"本次任务：\n{prompt}"
    )
    if provider == "codex":
        command = [executable, "exec", "--json", "--ephemeral", "--sandbox", "workspace-write", "-C", str(workspace), "-"]
        provider_label = "Codex"
    else:
        command = [executable, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits", "--max-turns", "8"]
        provider_label = "Claude Code"

    child_env = os.environ.copy()
    deepseek_key = child_env.get("DEEPSEEK_API_KEY")
    child_env.pop("DEEPSEEK_API_KEY", None)
    if deepseek_key and child_env.get("OPENAI_API_KEY") == deepseek_key:
        child_env.pop("OPENAI_API_KEY", None)
        if child_env.get("OPENAI_BASE_URL", "").startswith("https://api.deepseek.com"):
            child_env.pop("OPENAI_BASE_URL", None)

    store.record_event("agent.progress", info["name"], f"{info['name']} 已交给 {provider_label} 子 Runtime，在配置工作区中执行", project_id)
    process = await asyncio.create_subprocess_exec(
        *command, cwd=str(workspace), env=child_env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=1_000_000,
    )
    answers: list[str] = []
    plain_output: list[str] = []
    stderr_tail = bytearray()

    async def read_stdout() -> None:
        output_size = 0
        while line := await process.stdout.readline():
            output_size += len(line)
            if output_size > 5_000_000:
                continue
            raw_line = line.decode("utf-8", errors="replace").strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                plain_output.append(raw_line[:4000])
                continue
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type", ""))
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if event_type in {"item.started", "tool_use", "assistant"}:
                item_type = str(item.get("type", event.get("subtype", "工作步骤")))[:80]
                store.record_event("agent.progress", info["name"], f"{provider_label} 子 Runtime 正在运行：{item_type}", project_id)
            candidate = ""
            if event_type == "item.completed" and item.get("type") == "agent_message":
                candidate = external_message_text(item.get("text", item.get("content", "")))
            elif event_type == "assistant":
                candidate = external_message_text((event.get("message") or {}).get("content", ""))
            elif event_type == "result":
                candidate = external_message_text(event.get("result", event.get("content", "")))
            if candidate.strip():
                answers.append(candidate.strip())

    async def read_stderr() -> None:
        while chunk := await process.stderr.read(4096):
            stderr_tail.extend(chunk)
            if len(stderr_tail) > 12000:
                del stderr_tail[:-12000]

    stdout_task = asyncio.create_task(read_stdout())
    stderr_task = asyncio.create_task(read_stderr())
    try:
        process.stdin.write(complete_prompt.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()
        await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task, process.wait()), timeout=360)
    except asyncio.TimeoutError as error:
        process.kill()
        await process.wait()
        raise RuntimeError(f"{provider_label} 子 Runtime 执行超过 6 分钟，已停止本次运行。") from error
    finally:
        if process.stdin and not process.stdin.is_closing():
            process.stdin.close()
    if process.returncode:
        raise RuntimeError(f"{provider_label} 子 Runtime 退出码 {process.returncode}。请检查 CLI 登录状态和权限配置；原始 CLI 错误输出已隐藏。")
    answer = answers[-1] if answers else "\n".join(plain_output).strip()
    if not answer:
        raise RuntimeError(f"{provider_label} 子 Runtime 未返回可展示的文本，请检查 CLI 输出格式与登录状态。")
    store.record_event("agent.progress", info["name"], f"{info['name']} 已收到 {provider_label} 子 Runtime 报告", project_id)
    return answer[:30000]


async def invoke_role(project_id: str, role_id: str, prompt: str, history: list[dict[str, str]] | None = None) -> str:
    state = store.load(project_id) or {}
    role_runtime = (state.get("roleRuntimes") or {}).get(role_id) or {}
    if role_runtime.get("provider", "platform") != "platform":
        return await invoke_external_runtime(project_id, role_id, prompt, history)
    agent = build_agent(project_id, role_id)
    result: dict[str, Any] | None = None
    info = ROLE_INFO[role_id]
    agent_input = {"messages": history if history is not None else [{"role": "user", "content": prompt}]}
    try:
        async for snapshot in agent.astream(agent_input, stream_mode="values"):
            if not isinstance(snapshot, dict):
                continue
            result = snapshot
            messages = snapshot.get("messages", [])
            last = messages[-1] if messages else None
            tool_calls = getattr(last, "tool_calls", []) or []
            if tool_calls:
                names = "、".join(str(call.get("name", "项目工具")) for call in tool_calls[:3])
                progress = f"{info['name']} 正在执行项目步骤：{names}"
            elif last and last.__class__.__name__ == "ToolMessage":
                progress = f"{info['name']} 已完成一次项目操作，正在继续整理"
            else:
                progress = f"{info['name']} 正在分析项目上下文与下一步交付"
            store.record_event("agent.progress", info["name"], progress, project_id)
    except AttributeError:
        result = await agent.ainvoke(agent_input)
    if result is None:
        result = await agent.ainvoke(agent_input)
    for message in reversed(result.get("messages", [])):
        if message.__class__.__name__ == "AIMessage":
            text = message_content(message).strip()
            if text:
                return text
    return "我暂时没有生成文字回复，请重试。"


def state_messages_for_chat(state: dict[str, Any], room: str, incoming: str) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for item in state.get("chats", {}).get(room, [])[-20:]:
        if item.get("system"):
            continue
        content = str(item.get("text", ""))
        if content:
            history.append({"role": "user" if item.get("mine") else "assistant", "content": content})
    if not history or history[-1]["role"] != "user" or history[-1]["content"] != incoming:
        history.append({"role": "user", "content": incoming})
    return history[-20:]


def append_chat(state: dict[str, Any], room: str, message: dict[str, Any]) -> None:
    state.setdefault("chats", {}).setdefault(room, []).append(message)
    state["chats"][room] = state["chats"][room][-120:]


def create_requirement_plan_task(state: dict[str, Any], requirement: dict[str, Any]) -> dict[str, Any]:
    task = {
        "id": next_task_id(state),
        "title": f"PM 拆解 {requirement['id']} 的研发与验收任务",
        "req": requirement["id"], "state": "待开始", "owner": ROLE_INFO["pm"]["name"],
        "who": "PM", "color": ROLE_INFO["pm"]["color"], "due": "待排期",
        "acceptanceCriteria": "PM 需创建可执行的角色任务，覆盖开发、测试和验收，并更新关联交付文档。",
        "autoPlan": True,
    }
    state.setdefault("tasks", []).insert(0, task)
    requirement["deliveryPlanStatus"] = "queued"
    requirement["deliveryPlanTaskId"] = task["id"]
    state.setdefault("requirementPlans", {})[requirement["id"]] = {
        "status": "queued", "taskId": task["id"], "queuedAt": now_iso(),
        "attempts": int(state.setdefault("requirementPlans", {}).get(requirement["id"], {}).get("attempts", 0)) + 1,
    }
    record_task_change(state, task, f"需求 {requirement['id']} 已确认，PM 自动拆解已排队")
    return task


async def run_requirement_plan(project_id: str, requirement_id: str) -> None:
    """Run one persisted PM planning job; queued work survives a server restart."""
    global PLAN_LOCK
    if PLAN_LOCK is None:
        PLAN_LOCK = asyncio.Lock()
    async with PLAN_LOCK:
        state = store.load(project_id) or {}
        plan = state.setdefault("requirementPlans", {}).get(requirement_id)
        requirement = next((item for item in state.get("requirements", []) if item.get("id") == requirement_id), None)
        if not plan or plan.get("status") not in {"queued", "running"} or not requirement or requirement.get("state") != "已确认":
            return
        if not runtime_status()["agent_configured"]:
            return
        task_id = str(plan.get("taskId", ""))
        parent_task = next((task for task in state.get("tasks", []) if task.get("id") == task_id), None)
        if parent_task is None:
            parent_task = create_requirement_plan_task(state, requirement)
            plan = state["requirementPlans"][requirement_id]
            task_id = parent_task["id"]
        if parent_task.get("state") == "待开始":
            apply_task_transition(parent_task, "进行中", "PM 开始自动拆解已确认需求", ROLE_INFO["pm"]["name"])
            record_task_change(state, parent_task, f"PM 开始拆解 {requirement_id}")
        plan.update({"status": "running", "startedAt": plan.get("startedAt") or now_iso(), "error": ""})
        requirement["deliveryPlanStatus"] = "running"
        store.save(state, project_id, snapshot_actor=ROLE_INFO["pm"]["name"])
        store.record_event("requirement.plan_started", ROLE_INFO["pm"]["name"], f"开始拆解 {requirement_id}", project_id)

        project = store.get_project(project_id) or {}
        prompt = (
            f"系统已把需求 {requirement_id} 确认为正式范围。请立即为这一条需求完成可执行的交付拆解。\n"
            f"需求：{requirement.get('title', '')}\n用户问题：{requirement.get('subtitle', '')}\n"
            f"价值与证据：{requirement.get('value', '')}；{requirement.get('evidence', '')}；来源等级 {requirement.get('evidenceLevel', 'E0')}\n"
            f"优先级：{requirement.get('priority', 'P1')}；开发模式：{project.get('development_mode', '敏捷迭代')}。\n\n"
            "必须调用项目工具创建关联此需求的、带可验证验收标准的角色任务；按产品形态和开发模式选择必要角色，至少覆盖实际开发交付与 QA 验收，"
            "把依赖拆成清楚的先后关系（在任务标题或标准中说明），不要创建重复的 PM 占位任务。"
            "再读取并更新适用的 PRD、软件设计、测试大纲和自测大纲，写入需求编号、范围、方案和验收追踪。"
            "向项目群发布拆解摘要，指出负责人、依赖、风险、待用户决定事项。仅处理这条已批准需求，不增加未批准范围。"
            "如果信息缺口阻止准确拆解，先创建能安全推进的最小任务，并在摘要中明确列出需要用户决定的问题。"
        )
        try:
            summary = await invoke_role(project_id, "pm", prompt)
            state = store.load(project_id) or {}
            plan = state.setdefault("requirementPlans", {}).get(requirement_id, {})
            attempt = int(plan.get("attempts", 0))
            children = [
                task for task in state.get("tasks", [])
                if task.get("req") == requirement_id and task.get("id") != task_id and not task.get("autoPlan")
                and int(task.get("planAttempt", 0)) == attempt
            ]
            if not children:
                raise RuntimeError("PM 未创建关联研发 / 测试子任务；请检查模型工具调用后重试。")
            parent_task = next((task for task in state.get("tasks", []) if task.get("id") == task_id), None)
            if parent_task:
                if parent_task.get("state") == "进行中":
                    apply_task_transition(parent_task, "评审中", f"PM 已创建 {len(children)} 项关联交付任务，等待计划确认", ROLE_INFO["pm"]["name"])
                if parent_task.get("state") == "评审中":
                    apply_task_transition(parent_task, "已完成", f"已生成 {len(children)} 项角色任务并同步项目计划。", ROLE_INFO["pm"]["name"])
                record_task_change(state, parent_task, f"{requirement_id} 拆解完成，共 {len(children)} 项角色任务")
            plan.update({"status": "completed", "finishedAt": now_iso(), "summary": summary[:5000], "taskIds": [task.get("id") for task in children], "error": ""})
            requirement = next((item for item in state.get("requirements", []) if item.get("id") == requirement_id), requirement)
            requirement["deliveryPlanStatus"] = "completed"
            append_chat(state, "group", {
                "from": ROLE_INFO["pm"]["name"], "who": "PM", "color": ROLE_INFO["pm"]["color"],
                "time": datetime.now().astimezone().strftime("%H:%M"),
                "text": f"{requirement_id} 自动拆解完成：已创建 {len(children)} 项角色任务。\n\n{summary[:3500]}",
            })
            store.save(state, project_id, snapshot_actor=ROLE_INFO["pm"]["name"])
            store.record_event("requirement.plan_completed", ROLE_INFO["pm"]["name"], f"{requirement_id} 已拆解为 {len(children)} 项角色任务", project_id)
        except Exception as error:
            state = store.load(project_id) or {}
            plan = state.setdefault("requirementPlans", {}).get(requirement_id, {})
            parent_task = next((task for task in state.get("tasks", []) if task.get("id") == task_id), None)
            message = str(error)[:500]
            if parent_task and parent_task.get("state") in {"进行中", "待开始"}:
                try:
                    if parent_task.get("state") == "待开始":
                        apply_task_transition(parent_task, "进行中", "PM 自动拆解开始", ROLE_INFO["pm"]["name"])
                    apply_task_transition(parent_task, "有阻塞", message, ROLE_INFO["pm"]["name"])
                    record_task_change(state, parent_task, f"{requirement_id} 自动拆解受阻：{message}")
                except HTTPException:
                    pass
            plan.update({"status": "failed", "finishedAt": now_iso(), "error": message})
            requirement = next((item for item in state.get("requirements", []) if item.get("id") == requirement_id), None)
            if requirement:
                requirement["deliveryPlanStatus"] = "failed"
            append_chat(state, "group", {
                "from": ROLE_INFO["pm"]["name"], "who": "PM", "color": ROLE_INFO["pm"]["color"],
                "time": datetime.now().astimezone().strftime("%H:%M"),
                "text": f"{requirement_id} 自动拆解未完成，PM 任务已标为阻塞。原因：{message} 可打开需求详情重试。",
            })
            store.save(state, project_id, snapshot_actor=ROLE_INFO["pm"]["name"])
            store.record_event("requirement.plan_failed", ROLE_INFO["pm"]["name"], f"{requirement_id} 拆解失败：{message}", project_id)


async def automatic_plan_scheduler() -> None:
    while True:
        await asyncio.sleep(2)
        if not runtime_status()["agent_configured"]:
            continue
        for project in store.list_projects():
            project_id = project["project_id"]
            state = store.load(project_id) or {}
            for requirement_id, plan in state.get("requirementPlans", {}).items():
                if plan.get("status") in {"queued", "running"}:
                    try:
                        await run_requirement_plan(project_id, requirement_id)
                    except Exception as error:
                        state = store.load(project_id) or {}
                        current_plan = state.setdefault("requirementPlans", {}).get(requirement_id, {})
                        requirement = next((item for item in state.get("requirements", []) if item.get("id") == requirement_id), None)
                        message = f"PM 拆解调度异常：{str(error)[:400]}"
                        current_plan.update({"status": "failed", "finishedAt": now_iso(), "error": message})
                        if requirement:
                            requirement["deliveryPlanStatus"] = "failed"
                        store.save(state, project_id, snapshot_actor=ROLE_INFO["pm"]["name"])
                        store.record_event("requirement.plan_failed", ROLE_INFO["pm"]["name"], f"{requirement_id}：{message}", project_id)


async def run_project_meeting(
    project_id: str = "default", automatic: bool = False, topic: str = "项目进展与阻塞",
    agenda: list[str] | None = None,
) -> dict[str, Any]:
    global MEETING_LOCK
    if MEETING_LOCK is None:
        MEETING_LOCK = asyncio.Lock()
    async with MEETING_LOCK:
        status = runtime_status()
        if not status["agent_configured"]:
            raise HTTPException(status_code=503, detail={"message": "Deep Agents is not configured", **status})
        state = store.load(project_id) or {}
        default_agenda = state.get("meetingAgenda") or [
            "PDT：需要用户裁决的产品问题", "PM：需求、任务、交付物和依赖进度",
            "系统工程师：技术决策与变更影响", "各交付角色：下一步、风险与阻塞",
        ]
        selected_agenda = [str(item).strip()[:300] for item in (agenda if agenda is not None else default_agenda) if str(item).strip()][:10]
        if not selected_agenda:
            selected_agenda = default_agenda[:10]
        clean_topic = topic.strip()[:240] or "项目进展与阻塞"
        meeting_id = next_meeting_id(state)
        state["meetingLive"] = True
        meeting = {
            "id": meeting_id, "topic": clean_topic, "agenda": selected_agenda,
            "date": datetime.now().astimezone().strftime("%d"),
            "month": "今天 · 正在进行",
            "title": f"{clean_topic} · {len(state.get('meetings', [])) + 1} 轮",
            "summary": "各角色正在依序汇报进展、阻塞与待决策项",
            "duration": "实时会议", "attendees": [], "state": "进行中", "transcript": [],
            "decisionRequests": [], "deliveryOrder": [], "actionItems": [], "automatic": automatic,
            "startedAt": now_iso(),
        }
        state.setdefault("meetings", []).insert(0, meeting)
        store.save(state, project_id)
        store.record_event("meeting.started", "scheduler" if automatic else "user", meeting["title"], project_id)

        assignees = [task.get("who") for task in state.get("tasks", []) if task.get("state") in {"进行中", "评审中"}]
        short_to_id = {value["short"]: key for key, value in ROLE_INFO.items()}
        active_roles = [short_to_id.get(short) for short in assignees if short_to_id.get(short) and short_to_id.get(short) != "pm"]
        speakers = list(dict.fromkeys(["pdt"] + active_roles + ["se", "qa"]))[:8]
        contributions: list[str] = []
        failures: list[str] = []
        for role_id in speakers:
            info = ROLE_INFO[role_id]
            prompt = (
                f"你正在参加项目会议：{meeting['title']}。议题：{clean_topic}。\n"
                f"议程：{json.dumps(selected_agenda, ensure_ascii=False)}\n"
                "读取项目当前已确认需求、任务和决策，只汇报你的职责范围。用四个简短项目符号回答：已完成、下一步、阻塞 / 风险、需要的决策。"
                "这是会议发言，不得直接创建任务、批准需求或改写文档；把建议留给 PM 纪要并等待用户确认。"
            )
            try:
                text = await invoke_role(project_id, role_id, prompt)
            except Exception as error:
                text = f"本角色本轮发言失败：{str(error)[:300]}"
                failures.append(f"{info['name']}：{str(error)[:200]}")
            contribution = {"from": info["name"], "who": info["short"], "text": text}
            contributions.append(f"【{info['name']}】\n{text}")
            state = store.load(project_id) or state
            append_chat(state, "group", {
                "from": info["name"], "who": info["short"], "color": info["color"],
                "time": datetime.now().astimezone().strftime("%H:%M"), "text": text[:4000],
            })
            current_meeting = state.get("meetings", [{}])[0]
            current_meeting.setdefault("transcript", []).append(contribution)
            current_meeting["attendees"] = list(dict.fromkeys(current_meeting.get("attendees", []) + [info["short"]]))
            store.save(state, project_id)

        pm_prompt = (
            f"作为项目经理主持并收束会议 {meeting['title']}。\n主题：{clean_topic}\n议程：{json.dumps(selected_agenda, ensure_ascii=False)}\n"
            + "以下是本轮角色发言：\n\n" + "\n\n".join(contributions)
            + "\n\n请只返回合法 JSON，不要 Markdown 代码围栏，格式为："
            '{"summary":"纪要摘要","decisionRequests":["需要用户判断的问题"],"deliveryOrder":["交付顺序"],'
            '"actionItems":[{"title":"行动项","assigneeRole":"fe 角色 ID","requirementId":"已确认的 REQ 编号",'
            '"acceptanceCriteria":"可验证标准","due":"待排期","source":"依据的发言或议程"}]}。'
            "行动项只能基于已确认需求；不确定时 requirementId 留空，不能编造需求编号、事实或日期。"
            "行动项是 AI 建议，不能调用项目工具、创建正式任务、批准需求或修改文档；只有用户在界面确认后才会转任务。"
        )
        raw_minutes = ""
        try:
            raw_minutes = await invoke_role(project_id, "pm", pm_prompt)
        except Exception as error:
            failures.append(f"项目经理纪要：{str(error)[:200]}")
            raw_minutes = f"PM 纪要生成失败：{str(error)[:400]}"
        minutes = parse_meeting_minutes(raw_minutes, state)
        state = store.load(project_id) or state
        pm_group_text = (
            f"## {meeting_id} · {clean_topic}\n\n{minutes['summary']}\n\n"
            f"**待用户裁决：**{'；'.join(minutes['decisionRequests']) or '无'}\n\n"
            f"**交付顺序：**{'；'.join(minutes['deliveryOrder']) or '待明确'}\n\n"
            f"**行动建议：**{len(minutes['actionItems'])} 项（需在会议纪要中逐项确认后才会进入正式任务）"
        )
        append_chat(state, "group", {
            "from": ROLE_INFO["pm"]["name"], "who": "PM", "color": ROLE_INFO["pm"]["color"],
            "time": datetime.now().astimezone().strftime("%H:%M"), "text": pm_group_text[:5000],
        })
        final_meeting = next((item for item in state.get("meetings", []) if item.get("id") == meeting_id), meeting)
        partial = bool(failures)
        final_meeting.update({
            "summary": minutes["summary"][:600], "state": "部分完成" if partial else "纪要已生成",
            "duration": "由 Agent 角色完成", "pmSummary": minutes["summary"], "pmRaw": raw_minutes,
            "decisionRequests": minutes["decisionRequests"], "deliveryOrder": minutes["deliveryOrder"],
            "actionItems": minutes["actionItems"], "finishedAt": now_iso(), "errors": failures,
        })
        final_meeting["attendees"] = list(dict.fromkeys(final_meeting.get("attendees", []) + ["PM"]))
        state["meetingLive"] = False
        state["lastAutoMeetingAt"] = now_iso()
        if state.get("meetingEnabled"):
            state["nextAutoMeetingAt"] = (datetime.now(timezone.utc) + timedelta(minutes=int(state.get("cadence", 5)))).isoformat(timespec="seconds")
        action_text = "\n".join(
            f"- {item.get('id')} {item.get('title')} · {item.get('assigneeRole') or '待分配'} · {item.get('requirementId') or '未关联已确认需求'} · 待确认"
            for item in final_meeting.get("actionItems", [])
        ) or "- 暂无可执行行动项"
        doc_entry = (
            f"### {meeting_id} · {clean_topic}\n\n- 状态：{final_meeting['state']}\n- 议程：{'；'.join(selected_agenda)}\n"
            f"- 纪要：{minutes['summary'][:1200]}\n- 待用户裁决：{'；'.join(minutes['decisionRequests']) or '无'}\n"
            f"- 交付顺序：{'；'.join(minutes['deliveryOrder']) or '待明确'}\n- AI 行动项（均需用户确认）：\n{action_text}\n- 时间：{now_iso()}"
        )
        append_document_record(state, ["doc-2", "doc-4", "doc-5", "doc-6", "doc-9"], "## 会议纪要与行动项", doc_entry)
        bump_documents(state, ["doc-2", "doc-4", "doc-5", "doc-6", "doc-9"], f"记录项目会议 {meeting_id} 的纪要与行动建议")
        store.save(state, project_id, snapshot_actor=ROLE_INFO["pm"]["name"])
        event_type = "meeting.failed" if failures else "meeting.completed"
        store.record_event(event_type, ROLE_INFO["pm"]["name"], f"{meeting_id} · {final_meeting['state']} · {clean_topic}", project_id)
        return {"meeting": final_meeting, "state": state}


def fail_active_meeting(project_id: str, error: Exception, actor: str) -> None:
    state = store.load(project_id) or {}
    active = next((item for item in state.get("meetings", []) if item.get("state") == "进行中"), None)
    if not active:
        return
    active.update({"state": "失败", "finishedAt": now_iso()})
    active.setdefault("errors", []).append(str(error)[:500])
    state["meetingLive"] = False
    if state.get("meetingEnabled"):
        state["nextAutoMeetingAt"] = (datetime.now(timezone.utc) + timedelta(minutes=int(state.get("cadence", 5)))).isoformat(timespec="seconds")
    store.save(state, project_id, snapshot_actor=actor)
    store.record_event("meeting.failed", actor, str(error)[:300], project_id)


async def automatic_meeting_scheduler() -> None:
    while True:
        await asyncio.sleep(15)
        if not runtime_status()["agent_configured"]:
            continue
        for project in store.list_projects():
            project_id = project["project_id"]
            state = store.load(project_id) or {}
            if not state.get("meetingEnabled"):
                continue
            try:
                next_at = datetime.fromisoformat(state["nextAutoMeetingAt"])
            except (KeyError, ValueError):
                next_at = datetime.now(timezone.utc) + timedelta(minutes=int(state.get("cadence", 5)))
                state["nextAutoMeetingAt"] = next_at.isoformat(timespec="seconds")
                store.save(state, project_id)
            if datetime.now(timezone.utc) < next_at:
                continue
            try:
                await run_project_meeting(project_id=project_id, automatic=True)
            except Exception as error:
                fail_active_meeting(project_id, error, "scheduler")


def recover_interrupted_meetings() -> None:
    """Clear persisted in-progress meeting markers left behind by a stopped process."""
    for project in store.list_projects():
        project_id = project["project_id"]
        state = store.load(project_id) or {}
        meetings = state.get("meetings", [])
        active = [item for item in meetings if isinstance(item, dict) and item.get("state") == "进行中"]
        if not active and not state.get("meetingLive"):
            continue

        recovered_at = now_iso()
        for meeting in active:
            meeting.update({"state": "失败", "finishedAt": recovered_at})
            errors = meeting.get("errors")
            if not isinstance(errors, list):
                errors = []
                meeting["errors"] = errors
            errors.append("服务进程已停止，本轮会议被中断；已保留已保存的发言，请重新发起。")

        state["meetingLive"] = False
        if state.get("meetingEnabled"):
            try:
                cadence = max(3, min(120, int(state.get("cadence", 5))))
            except (TypeError, ValueError):
                cadence = 5
            state["nextAutoMeetingAt"] = (
                datetime.now(timezone.utc) + timedelta(minutes=cadence)
            ).isoformat(timespec="seconds")
        else:
            state.pop("nextAutoMeetingAt", None)

        store.save(state, project_id, snapshot_actor="system")
        if active:
            for meeting in active:
                store.record_event(
                    "meeting.failed", "system",
                    f"{meeting.get('id', 'MEET')} · 服务进程退出，中断会议状态已恢复",
                    project_id,
                )
        else:
            store.record_event(
                "meeting.failed", "system",
                "检测到遗留的会议运行标记，已清理并恢复自动会议调度",
                project_id,
            )


@asynccontextmanager
async def lifespan(_: FastAPI):
    for project in store.list_projects():
        state = store.load(project["project_id"]) or {}
        if migrate_fact_audit(state):
            store.save(state, project["project_id"], snapshot_actor="项目记忆审计迁移")
            store.record_event("fact.audit_migrated", "system", "为历史项目记忆补充审计基线和来源等级字段", project["project_id"])
    recover_interrupted_meetings()
    meeting_task = asyncio.create_task(automatic_meeting_scheduler())
    plan_task = asyncio.create_task(automatic_plan_scheduler())
    try:
        yield
    finally:
        for task in (meeting_task, plan_task):
            task.cancel()
        for task in (meeting_task, plan_task):
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="织点 AI 研发协作", version="0.1.0", lifespan=lifespan)


def require_project(project_id: str) -> None:
    if not store.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return runtime_status()


@app.get("/api/projects")
def get_projects() -> list[dict[str, Any]]:
    return store.list_projects()


@app.get("/api/projects/{project_id}/export")
def export_project(project_id: str) -> dict[str, Any]:
    require_project(project_id)
    project = store.get_project(project_id)
    state = store.load(project_id) or {}
    with STORE_LOCK, store._connect() as connection:
        event_rows = connection.execute(
            "SELECT id,event_type,actor,summary,created_at FROM project_events WHERE project_id=? ORDER BY id",
            (project_id,),
        ).fetchall()
        version_rows = connection.execute(
            "SELECT id,document_id,version,actor,summary,content_json,created_at FROM document_versions WHERE project_id=? ORDER BY id",
            (project_id,),
        ).fetchall()
    versions = [dict(row) for row in version_rows]
    for version in versions:
        version["content"] = json.loads(version.pop("content_json"))
    return {
        "schemaVersion": 1,
        "exportedAt": now_iso(),
        "project": project,
        "state": state,
        "events": [dict(row) for row in event_rows],
        "documentVersions": versions,
        "restoreSupported": False,
    }


@app.delete("/api/projects/{project_id}")
@synchronized
def delete_project(project_id: str, request: ProjectDeleteRequest) -> dict[str, Any]:
    require_project(project_id)
    if project_id == "default":
        raise HTTPException(status_code=409, detail="默认项目不能通过工作台删除")
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if request.confirm_name.strip() != project["name"]:
        raise HTTPException(status_code=409, detail="输入的项目名称与当前项目不一致")
    store.delete_project(project_id)
    for key in [key for key in AGENT_CACHE if key and key[0] == project_id]:
        AGENT_CACHE.pop(key, None)
    return {"deleted": True, "project_id": project_id, "project_name": project["name"]}


@app.post("/api/projects")
def post_project(request: ProjectCreateRequest) -> dict[str, Any]:
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Project name is required")
    project = store.create_project(name, request.description.strip(), request.product_shape.strip(), request.development_mode.strip())
    state = initial_project_state(project)
    store.save(state, project["project_id"], snapshot_actor="system")
    store.record_event("project.created", "user", f"创建项目：{name}", project["project_id"])
    project.update({"requirement_count": 0, "task_count": 0})
    return {"project": project, "state": state}


@app.get("/api/state")
def get_state(project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    require_project(project_id)
    return store.load(project_id) or {}


@app.get("/api/requirements/{requirement_id}/trace")
def requirement_trace(requirement_id: str, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    """Return the project records directly linked to one requirement."""
    require_project(project_id)
    state = store.load(project_id) or {}
    requirement = next((item for item in state.get("requirements", []) if item.get("id") == requirement_id), None)
    if requirement is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    requirement_aliases = {requirement_id}
    if requirement.get("draftId"):
        requirement_aliases.add(str(requirement["draftId"]))
    related_facts = [fact for fact in state.get("facts", []) if str(fact.get("requirementId", "")) in requirement_aliases]
    fact_document_ids = {str(fact.get("documentId")) for fact in related_facts if fact.get("documentId")}
    related_documents = []
    for document in state.get("docs", []):
        content = str(document.get("content", ""))
        if any(alias in content or alias in str(document.get("lastChange", "")) for alias in requirement_aliases) or str(document.get("id")) in fact_document_ids:
            related_documents.append({key: document.get(key) for key in ("id", "name", "version", "state", "lastChange")})
    decisions = [
        item for item in state.get("projectDecisions", [])
        if str(item.get("requirement", "")) in requirement_aliases
        or any(alias in " ".join(str(item.get(key, "")) for key in ("title", "proposal", "rationale")) for alias in requirement_aliases)
    ]
    human_decision = None
    if requirement.get("humanDecision"):
        human_decision = {
            "decision": requirement.get("humanDecision"), "actor": "项目发起人",
            "createdAt": requirement.get("humanDecisionAt"),
            "note": requirement.get("decisionNote") or requirement.get("approvalNote", ""),
        }
    return {
        "requirement": {key: requirement.get(key) for key in ("id", "draftId", "draftVersion", "baselineVersion", "title", "state", "humanDecision", "humanDecisionAt", "decisionNote")},
        "human_decision": human_decision,
        "proposed_decisions": decisions,
        "facts": related_facts,
        "tasks": [task for task in state.get("tasks", []) if str(task.get("req", "")) in requirement_aliases],
        "documents": related_documents,
        "analysis_history": requirement.get("analysisHistory", []),
        "decision_history": requirement.get("decisionHistory", []),
        "clarification_history": requirement.get("clarificationHistory", []),
    }


@app.get("/api/facts/{fact_id}/trace")
def project_fact_trace(fact_id: str, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    """Return explicit fact links and records that cite the fact ID; chat is never indexed as evidence."""
    require_project(project_id)
    state = store.load(project_id) or {}
    fact = next((item for item in state.get("facts", []) if item.get("id") == fact_id), None)
    if fact is None:
        raise HTTPException(status_code=404, detail="Project memory not found")

    def cites(record: dict[str, Any]) -> bool:
        for key in ("factId", "fact_id", "factIds", "fact_ids", "evidenceFactIds", "memoryRefs"):
            value = record.get(key)
            if value == fact_id or isinstance(value, list) and fact_id in {str(item) for item in value}:
                return True
        return False

    def mentions(value: Any) -> bool:
        return bool(re.search(rf"(?<![A-Za-z0-9_-]){re.escape(fact_id)}(?![A-Za-z0-9_-])", str(value or "")))

    requirement_id = str(fact.get("requirementId", ""))
    task_id = str(fact.get("taskId", ""))
    document_id = str(fact.get("documentId", ""))
    requirement = next((item for item in state.get("requirements", []) if requirement_id and requirement_id in {str(item.get("id", "")), str(item.get("draftId", ""))}), None)
    task = next((item for item in state.get("tasks", []) if task_id and str(item.get("id", "")) == task_id), None)
    document = next((item for item in state.get("docs", []) if document_id and str(item.get("id", "")) == document_id), None)
    linked_docs = [item for item in state.get("docs", []) if cites(item) or mentions(item.get("content", ""))]
    linked_requirements = [item for item in state.get("requirements", []) if cites(item)]
    linked_tasks = [item for item in state.get("tasks", []) if cites(item)]
    linked_decisions = [item for item in state.get("projectDecisions", []) if cites(item)]
    linked_meetings = [
        item for item in state.get("meetings", [])
        if cites(item) or any(cites(action) for action in item.get("actionItems", []) if isinstance(action, dict))
    ]
    related_by_requirement = [
        item for item in state.get("tasks", [])
        if requirement and str(item.get("req", "")) in {str(requirement.get("id", "")), str(requirement.get("draftId", ""))}
        and str(item.get("id", "")) != task_id and not cites(item)
    ]
    return {
        "fact": fact,
        "links": {"requirement": requirement, "task": task, "document": document},
        "referenced_by": {
            "requirements": linked_requirements, "tasks": linked_tasks, "decisions": linked_decisions,
            "documents": linked_docs, "meetings": linked_meetings,
        },
        "related_by_requirement": related_by_requirement,
        "evidence_boundary": "只纳入结构化关联或明确引用编号的记录；聊天文本不作为项目事实或证据来源。",
    }


@app.post("/api/decisions/{decision_id}/review")
@synchronized
def review_project_decision(
    decision_id: str, request: ProjectDecisionReviewRequest,
    project_id: str = Query(default="default", min_length=1, max_length=80),
) -> dict[str, Any]:
    """Record the user's review of a proposed decision without changing requirement scope or creating tasks."""
    require_project(project_id)
    state = store.load(project_id) or {}
    decision = next((item for item in state.get("projectDecisions", []) if item.get("id") == decision_id), None)
    if decision is None:
        raise HTTPException(status_code=404, detail="Project decision not found")
    if decision.get("humanDecision"):
        if decision.get("humanDecision") == request.decision and decision.get("decisionNote") == request.note.strip():
            return {"decision": decision, "state": state, "idempotent": True}
        raise HTTPException(status_code=409, detail="此决策建议已审阅；如需重新评估，请另建一条建议并关联原编号")
    if decision.get("state") != "待 PDT 与用户确认":
        raise HTTPException(status_code=409, detail="只有等待用户审阅的决策建议可以裁决")
    decision.setdefault("version", 1)
    decision.setdefault("history", [{
        "version": 1, "action": "历史基线", "actor": decision.get("proposedBy", "迁移记录"),
        "state": "待 PDT 与用户确认", "summary": "补齐决策审计基线", "createdAt": decision.get("createdAt", now_iso()),
    }])
    decision.update({
        "state": request.decision, "humanDecision": request.decision,
        "reviewedBy": "项目发起人", "reviewedAt": now_iso(), "decisionNote": request.note.strip(),
        "version": int(decision.get("version", 1)) + 1,
    })
    decision.setdefault("history", []).append({
        "version": decision["version"], "action": request.decision, "actor": decision["reviewedBy"],
        "state": decision["state"], "previousState": "待 PDT 与用户确认", "summary": request.note.strip()[:200],
        "note": request.note.strip(), "createdAt": decision["reviewedAt"],
    })
    append_document_record(
        state, ["doc-9"], "## 用户决策审阅记录",
        f"### {decision_id} · {request.decision} · v{decision['version']}\n\n- 建议：{decision.get('title', '')}\n"
        f"- 提案：{decision.get('proposal', '')}\n- 原因：{request.note.strip()}\n- 审阅人：{decision['reviewedBy']}\n- 时间：{decision['reviewedAt']}\n"
        "- 边界：该决定审阅不替代需求基线批准，也不会自动创建任务。",
    )
    append_chat(state, "group", {
        "from": "项目发起人", "who": "Z", "color": "#bd9773", "time": datetime.now().astimezone().strftime("%H:%M"),
        "text": f"用户将决策建议 {decision_id}「{decision.get('title', '')}」审阅为{request.decision}。理由：{request.note.strip()} 此裁决不自动建立需求基线或开发任务。",
    })
    store.save(state, project_id, snapshot_actor="项目发起人")
    store.record_event("decision.reviewed", "user", f"{decision_id}：{request.decision} · {request.note[:120]}", project_id)
    return {"decision": decision, "state": state, "idempotent": False}


@app.put("/api/state")
@synchronized
def put_state(payload: StatePayload, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, str]:
    require_project(project_id)
    project = store.get_project(project_id)
    current_state = store.load(project_id) or {}
    # These records are updated through dedicated server actions. Preserve them when
    # an older browser tab submits a whole-state snapshot that predates newer fields.
    for field in ("facts", "requirementPlans", "projectDecisions", "meetings", "meetingAgenda", "cadence", "meetingEnabled", "meetingLive", "lastAutoMeetingAt", "nextAutoMeetingAt", "memberCounts", "rolePrompts", "roleRuntimes", "reportingLines", "maxParallelTasks"):
        if field in current_state:
            payload.state[field] = current_state[field]
        elif field in {"lastAutoMeetingAt", "nextAutoMeetingAt"}:
            payload.state.pop(field, None)
    # Dedicated requirement actions own requirement identity and history. Keeping the
    # authoritative list also prevents a stale tab from resurrecting a DRAFT-* entry
    # after approval has assigned its canonical REQ-* baseline identity.
    if current_state.get("requirements"):
        payload.state["requirements"] = current_state["requirements"]
    current_documents = {str(item.get("id")): item for item in current_state.get("docs", [])}
    incoming_documents = payload.state.setdefault("docs", [])
    for document_id, current_document in current_documents.items():
        incoming_index = next((index for index, item in enumerate(incoming_documents) if str(item.get("id")) == document_id), None)
        if incoming_index is None:
            incoming_documents.append(current_document)
        else:
            # Agent/API document updates and their versioned snapshots must survive
            # whole-state writes from a tab that was opened before the update.
            incoming_documents[incoming_index] = current_document
    # Task lifecycle and meeting action promotion use dedicated API routes. Keep the
    # server list authoritative so an old browser snapshot cannot erase new tasks.
    if "tasks" in current_state:
        payload.state["tasks"] = current_state["tasks"]
    requested_mode = str(payload.state.get("mode", project["development_mode"]))
    if requested_mode not in {"敏捷迭代", "瀑布式", "混合模式"}:
        raise HTTPException(status_code=422, detail="Unknown development mode")
    if requested_mode != project["development_mode"]:
        with STORE_LOCK, store._connect() as connection:
            connection.execute("UPDATE projects SET development_mode=?,updated_at=? WHERE project_id=?", (requested_mode, now_iso(), project_id))
        store.record_event("project.mode_changed", "user", f"开发模式调整为{requested_mode}", project_id)
    payload.state["projectId"] = project_id
    payload.state["projectName"] = project["name"]
    payload.state["productShape"] = project["product_shape"]
    payload.state["mode"] = requested_mode
    store.save(payload.state, project_id, snapshot_actor="user")
    return {"status": "saved", "updated_at": now_iso()}


@app.post("/api/facts")
def create_project_fact(request: ProjectFactCreateRequest, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    """Record a user-supplied project fact, decision, evidence item, assumption, or risk."""
    require_project(project_id)
    if min(len(request.title.strip()), len(request.content.strip()), len(request.source.strip())) < 3:
        raise HTTPException(status_code=422, detail="标题、内容与来源去除空白后都至少需要 3 个字符")
    with STORE_LOCK:
        state = store.load(project_id) or {}
        related_ids = {
            "requirement_id": {str(item.get("id")) for item in state.get("requirements", [])},
            "task_id": {str(item.get("id")) for item in state.get("tasks", [])},
            "document_id": {str(item.get("id")) for item in state.get("docs", [])},
        }
        for key, valid_ids in related_ids.items():
            value = str(getattr(request, key)).strip()
            if value and value not in valid_ids:
                raise HTTPException(status_code=422, detail=f"关联对象不存在：{value}")
        fact, duplicate = create_project_fact_record(
            state, kind=request.kind, title=request.title, content=request.content, source=request.source,
            created_by="项目发起人", status="用户提供", confidence=0,
            requirement_id=request.requirement_id, task_id=request.task_id, document_id=request.document_id,
            evidence_level=request.evidence_level,
        )
        if not duplicate:
            append_chat(state, "group", {
                "from": "项目发起人", "who": "Z", "color": "#7a8290",
                "time": datetime.now().astimezone().strftime("%H:%M"),
                "text": f"已将 {fact['id']}「{fact['title']}」加入项目记忆（{fact['kind']}）。来源：{fact['source']}",
            })
            store.save(state, project_id, snapshot_actor="项目发起人")
            store.record_event("fact.created", "user", f"新增项目记忆 {fact['id']}：{fact['title']}", project_id)
        return {"fact": fact, "duplicate": duplicate, "state": state}


@app.patch("/api/facts/{fact_id}")
def review_project_fact(fact_id: str, request: ProjectFactReviewRequest, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    """Confirm an AI-proposed memory or retire a memory while preserving its history."""
    require_project(project_id)
    with STORE_LOCK:
        state = store.load(project_id) or {}
        fact = next((item for item in state.get("facts", []) if item.get("id") == fact_id), None)
        if fact is None:
            raise HTTPException(status_code=404, detail="Project memory not found")
        if fact.get("status") == "已取代":
            raise HTTPException(status_code=409, detail="该项目记忆已经标为已取代")
        if request.decision == "用户确认" and fact.get("status") != "待核实":
            raise HTTPException(status_code=409, detail="只有待核实的 AI 记忆可以确认为项目上下文")
        previous_status = fact.get("status", "待核实")
        fact.setdefault("version", 1)
        fact.setdefault("history", [])
        if not fact["history"]:
            fact["history"].append({
                "version": fact["version"], "actor": fact.get("createdBy", "迁移记录"), "action": "迁移",
                "summary": "补齐项目记忆审计基线", "status": previous_status,
                "evidenceLevel": fact.get("evidenceLevel", "E0"), "confidence": fact.get("confidence", 0),
                "createdAt": fact.get("createdAt", now_iso()),
            })
        fact["status"] = request.decision
        fact["reviewedAt"] = now_iso()
        fact["reviewNote"] = request.note.strip()
        fact["reviewedBy"] = "项目发起人"
        fact["updatedAt"] = fact["reviewedAt"]
        fact["version"] = int(fact.get("version", 1)) + 1
        fact["changeSummary"] = (request.change_summary.strip() or f"项目发起人将状态从{previous_status}更新为{request.decision}")[:200]
        fact["history"].append({
            "version": fact["version"], "actor": fact["reviewedBy"], "action": request.decision,
            "summary": fact["changeSummary"], "previousStatus": previous_status,
            "status": fact["status"], "evidenceLevel": fact.get("evidenceLevel", "E0"),
            "confidence": fact.get("confidence", 0), "note": fact["reviewNote"],
            "createdAt": fact["reviewedAt"],
        })
        text = f"用户将 {fact_id}「{fact.get('title', '')}」标记为{request.decision}。"
        if request.note.strip():
            text += f" 说明：{request.note.strip()}"
        append_document_record(state, ["doc-9"], "## 项目记忆审阅记录", f"### {fact_id} · v{fact['version']} · {request.decision}\n\n- 标题：{fact.get('title', '')}\n- 变更摘要：{fact['changeSummary']}\n- 证据等级：{fact.get('evidenceLevel', 'E0')}\n- 理由：{request.note.strip() or '（未填写）'}\n- 审阅人：{fact['reviewedBy']}\n- 时间：{fact['reviewedAt']}")
        bump_documents(state, ["doc-9"], f"审阅项目记忆 {fact_id}：{request.decision}")
        append_chat(state, "group", {
            "from": "项目发起人", "who": "Z", "color": "#7a8290",
            "time": datetime.now().astimezone().strftime("%H:%M"), "text": text,
        })
        store.save(state, project_id, snapshot_actor="项目发起人")
        event_type = "fact.confirmed" if request.decision == "用户确认" else "fact.retired"
        store.record_event(event_type, "user", text[:180], project_id)
        return {"fact": fact, "state": state}


@app.get("/api/events")
def get_events(limit: int = 40, project_id: str = Query(default="default", min_length=1, max_length=80)) -> list[dict[str, Any]]:
    require_project(project_id)
    return store.events(project_id=project_id, limit=max(1, min(limit, 100)))


@app.get("/api/events/stream")
async def stream_events(request: Request, project_id: str = Query(default="default", min_length=1, max_length=80)) -> StreamingResponse:
    require_project(project_id)
    last_event_id = request.headers.get("last-event-id", "").strip()
    try:
        cursor = max(0, int(last_event_id)) if last_event_id else 0
    except ValueError:
        cursor = 0
    if not last_event_id:
        with STORE_LOCK, store._connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(id),0) AS id FROM project_events WHERE project_id=?", (project_id,)).fetchone()
        cursor = int(row["id"] if row else 0)

    async def event_stream():
        nonlocal cursor
        yield "retry: 3000\n\n"
        keepalive = 0
        while True:
            for event in store.events_after(project_id, cursor):
                cursor = int(event["id"])
                yield f"id: {cursor}\nevent: project-event\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                keepalive = 0
            keepalive += 1
            if keepalive >= 15:
                yield ": keep-alive\n\n"
                keepalive = 0
            await asyncio.sleep(1)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.put("/api/roadmap/settings")
def update_roadmap_settings(request: RoadmapSettingsRequest, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    require_project(project_id)
    with STORE_LOCK:
        state = store.load(project_id) or {}
        active = [task for task in state.get("tasks", []) if task.get("state") == "进行中"]
        if request.max_parallel_tasks < len(active):
            raise HTTPException(status_code=409, detail=f"已有 {len(active)} 项任务正在进行，不能把并行上限调低到 {request.max_parallel_tasks}")
        previous = int(state.get("maxParallelTasks", 3) or 3)
        state["maxParallelTasks"] = request.max_parallel_tasks
        message = f"项目并行任务上限由 {previous} 调整为 {request.max_parallel_tasks}"
        append_chat(state, "group", {
            "from": "项目发起人", "who": "Z", "color": "#bd9773",
            "time": datetime.now().astimezone().strftime("%H:%M"), "text": message,
        })
        store.save(state, project_id, snapshot_actor="user")
        store.record_event("roadmap.parallel_limit_changed", "user", message, project_id)
        return {"state": state, "max_parallel_tasks": request.max_parallel_tasks}


@app.get("/api/documents/{document_id}/versions")
def get_document_versions(document_id: str, project_id: str = Query(default="default", min_length=1, max_length=80)) -> list[dict[str, Any]]:
    require_project(project_id)
    state = store.load(project_id) or {}
    if not any(document.get("id") == document_id for document in state.get("docs", [])):
        raise HTTPException(status_code=404, detail="Document not found")
    return store.document_versions(project_id, document_id)


@app.post("/api/documents/{document_id}/restore")
def restore_document_version(document_id: str, request: DocumentRestoreRequest, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    require_project(project_id)
    state = store.load(project_id) or {}
    document_index = next((i for i, document in enumerate(state.get("docs", [])) if document.get("id") == document_id), None)
    if document_index is None:
        raise HTTPException(status_code=404, detail="Document not found")
    snapshot = store.document_version(project_id, document_id, request.version_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Document version not found")
    current = state["docs"][document_index]
    restored = dict(snapshot["content"])
    try:
        next_version = float(str(current.get("version", "v0.1"))[1:]) + 0.1
    except (TypeError, ValueError):
        next_version = 0.2
    restored.update({
        "id": document_id, "version": f"v{next_version:.1f}", "updated": "刚刚", "state": "评审中",
        "lastChange": f"从 {snapshot['version']} 恢复：{snapshot['summary']}",
    })
    state["docs"][document_index] = restored
    store.save(state, project_id, snapshot_actor="user")
    store.record_event("document.restored", "user", f"恢复 {restored.get('name')} 至 {snapshot['version']}，新版本 {restored['version']}", project_id)
    return {"state": state, "document": restored}


@app.post("/api/chat")
async def chat(request: ChatRequest) -> dict[str, Any]:
    require_project(request.project_id)
    room = request.role if request.role in ROLE_INFO or request.role in {"group", "dev"} else "group"
    role_id = as_chat_role(room)
    state = store.load(request.project_id) or {}
    info = ROLE_INFO[role_id]
    history = state_messages_for_chat(state, room, request.message)
    chat_list = state.get("chats", {}).get(room, [])
    if not chat_list or chat_list[-1].get("text") != request.message or not chat_list[-1].get("mine"):
        append_chat(state, room, {"mine": True, "from": "张同学", "who": "Z", "time": "刚刚", "text": request.message})
        store.save(state, request.project_id)
    status = runtime_status()
    role_runtime = (state.get("roleRuntimes") or {}).get(role_id) or {"provider": "platform"}
    selected_provider = role_runtime.get("provider", "platform")
    status["selected_runtime"] = selected_provider
    if selected_provider == "platform" and not status["agent_configured"]:
        reply = "后端已连接，项目消息已保存。要启用 DeepSeek 驱动的 Deep Agents 回复，请在项目 `.env` 配置 `DEEPSEEK_API_KEY`，然后重启服务。"
        live = False
    else:
        store.record_event("agent.run_started", info["name"], f"{info['name']} 开始处理用户请求", request.project_id)
        try:
            reply = await invoke_role(request.project_id, role_id, request.message, history)
            live = True
        except Exception as error:
            store.record_event("agent.run_failed", info["name"], f"{info['name']} 处理失败；请查看协作消息中的错误提示", request.project_id)
            raise HTTPException(status_code=502, detail=f"{selected_provider} Runtime 调用失败：{str(error)[:500]}") from error
    state = store.load(request.project_id) or state
    append_chat(state, room, {
        "from": info["name"], "who": info["short"], "color": info["color"],
        "time": datetime.now().astimezone().strftime("%H:%M"), "text": reply,
    })
    if room != "group":
        append_chat(state, "group", {
            "from": info["name"], "who": info["short"], "color": info["color"],
            "time": datetime.now().astimezone().strftime("%H:%M"),
            "text": f"我已完成项目发起人的私聊请求，以下同步结论与待处理事项：\n\n{reply[:3500]}",
        })
    store.save(state, request.project_id)
    store.record_event("agent.reply", info["name"], request.message[:160], request.project_id)
    return {"reply": reply, "live": live, "state": state, "runtime": status}


@app.post("/api/requirements/{requirement_id}/approve")
def approve_requirement(requirement_id: str, payload: ApprovalPayload, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    """Human-only route. No model-facing tool can call this approval endpoint."""
    require_project(project_id)
    note = payload.note.strip()
    if len(note) < 3:
        raise HTTPException(status_code=422, detail="请至少填写 3 个字符的确认依据")
    with STORE_LOCK:
        state = store.load(project_id) or {}
        requirement = next((r for r in state.get("requirements", []) if r.get("id") == requirement_id), None)
        if requirement is None:
            raise HTTPException(status_code=404, detail="Requirement not found")
        if requirement.get("state") not in {"待你裁决", "挂起待证据"}:
            raise HTTPException(status_code=409, detail="只有待裁决或挂起待证据的草稿可以确认纳入范围")
        before = requirement_draft_snapshot(requirement)
        draft_id = str(requirement.get("id", ""))
        if draft_id.startswith("DRAFT-"):
            requirement["draftId"] = draft_id
            requirement["id"] = next_baseline_requirement_id(state)
            requirement["baselineVersion"] = 1
            requirement["baselineApprovedAt"] = now_iso()
        else:
            requirement["baselineVersion"] = int(requirement.get("baselineVersion", 1))
        requirement["state"] = "已确认"
        requirement["owner"] = ROLE_INFO["pdt"]["name"]
        requirement["humanApprovedAt"] = now_iso()
        requirement["approvalNote"] = note
        requirement["humanDecision"] = "已确认"
        requirement["humanDecisionAt"] = requirement["humanApprovedAt"]
        requirement["decisionNote"] = note
        append_requirement_decision_history(requirement, action="批准", note=note, actor="项目发起人", before=before)
        record_requirement_decision(state, requirement, "已确认纳入范围", note)
        task = create_requirement_plan_task(state, requirement)
        append_chat(state, "group", {
            "from": ROLE_INFO["pdt"]["name"], "who": "PDT", "color": ROLE_INFO["pdt"]["color"],
            "time": datetime.now().astimezone().strftime("%H:%M"),
            "text": f"{draft_id} 已由用户确认并转为 {requirement['id']} v{requirement['baselineVersion']}。PM 拆解已进入自动队列；模型就绪后会生成角色任务、验收标准并更新交付文档。",
        })
        store.save(state, project_id, snapshot_actor="user")
        store.record_event("requirement.approved", "user", f"确认 {draft_id} → {requirement['id']} v{requirement['baselineVersion']}", project_id)
        store.record_event("requirement.plan_queued", ROLE_INFO["pm"]["name"], f"{requirement['id']} 自动拆解已排队", project_id)
        return {"state": state, "task": task, "requirement": requirement, "draft_id": draft_id}


@app.post("/api/requirements/{requirement_id}/plan/retry")
def retry_requirement_plan(requirement_id: str, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    """Queue a failed PM breakdown again without changing the human-approved scope."""
    require_project(project_id)
    with STORE_LOCK:
        state = store.load(project_id) or {}
        requirement = next((item for item in state.get("requirements", []) if item.get("id") == requirement_id), None)
        if requirement is None:
            raise HTTPException(status_code=404, detail="Requirement not found")
        if requirement.get("state") != "已确认":
            raise HTTPException(status_code=409, detail="只有已确认需求可以启动 PM 拆解")
        plan = state.setdefault("requirementPlans", {}).get(requirement_id, {})
        if plan.get("status") in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="PM 拆解已经在队列中或正在运行")
        task = create_requirement_plan_task(state, requirement)
        append_chat(state, "group", {
            "from": ROLE_INFO["pdt"]["name"], "who": "PDT", "color": ROLE_INFO["pdt"]["color"],
            "time": datetime.now().astimezone().strftime("%H:%M"),
            "text": f"已按请求重新排队拆解 {requirement_id}；PM 会继续同步拆解进度。",
        })
        store.save(state, project_id, snapshot_actor="user")
        store.record_event("requirement.plan_queued", ROLE_INFO["pm"]["name"], f"重新排队拆解 {requirement_id}", project_id)
        return {"state": state, "task": task}


@app.post("/api/requirements/{requirement_id}/decision")
def decide_requirement(requirement_id: str, payload: RequirementDecisionPayload, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    """Persist a human rejection or deferral; these outcomes never create delivery tasks."""
    require_project(project_id)
    note = payload.note.strip()
    if len(note) < 3:
        raise HTTPException(status_code=422, detail="请至少填写 3 个字符的裁决理由")
    with STORE_LOCK:
        state = store.load(project_id) or {}
        requirement = next((r for r in state.get("requirements", []) if r.get("id") == requirement_id), None)
        if requirement is None:
            raise HTTPException(status_code=404, detail="Requirement not found")
        if requirement.get("state") not in {"待你裁决", "挂起待证据"}:
            raise HTTPException(status_code=409, detail="只有待裁决或挂起待证据的需求可以继续裁决")
        before = requirement_draft_snapshot(requirement)
        requirement["state"] = payload.decision
        requirement["owner"] = ROLE_INFO["pdt"]["name"]
        requirement["humanDecision"] = payload.decision
        requirement["humanDecisionAt"] = now_iso()
        requirement["decisionNote"] = note
        append_requirement_decision_history(requirement, action=payload.decision, note=note, actor="项目发起人", before=before)
        record_requirement_decision(state, requirement, payload.decision, note)
        verb = "拒绝" if payload.decision == "已拒绝" else "挂起待证据" if payload.decision == "挂起待证据" else "搁置"
        append_chat(state, "group", {
            "from": ROLE_INFO["pdt"]["name"], "who": "PDT", "color": ROLE_INFO["pdt"]["color"],
            "time": datetime.now().astimezone().strftime("%H:%M"),
            "text": f"用户已{verb} {requirement_id}「{requirement.get('title', '')}」。理由：{note}",
        })
        store.save(state, project_id, snapshot_actor="user")
        event_type = "requirement.rejected" if payload.decision == "已拒绝" else "requirement.evidence_held" if payload.decision == "挂起待证据" else "requirement.deferred"
        store.record_event(event_type, "user", f"{verb} {requirement_id}：{note[:150]}", project_id)
        return {"state": state, "requirement": requirement}


@app.post("/api/requirements/{requirement_id}/revise")
def revise_requirement(requirement_id: str, payload: RequirementRevisionPayload, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    """Save a human-edited candidate revision and return it to the decision queue without deriving tasks."""
    require_project(project_id)
    required_fields = {
        "标题": payload.title, "用户问题": payload.user_problem,
        "合理性评估": payload.rationality_assessment, "价值假设": payload.value_hypothesis,
        "当前证据": payload.evidence, "证据缺口": payload.evidence_gap,
        "Kano 判断依据": payload.classification_rationale, "修改理由": payload.note,
    }
    missing = [label for label, value in required_fields.items() if len(value.strip()) < 3]
    if missing:
        raise HTTPException(status_code=422, detail="需求字段去除空白后至少填写 3 个字符：" + "、".join(missing))
    with STORE_LOCK:
        state = store.load(project_id) or {}
        requirement = next((item for item in state.get("requirements", []) if item.get("id") == requirement_id), None)
        if requirement is None:
            raise HTTPException(status_code=404, detail="Requirement not found")
        if requirement.get("lastRevisionActionId") == payload.action_id:
            return {"state": state, "requirement": requirement, "duplicate": True}
        if requirement.get("state") not in {"待你裁决", "挂起待证据"}:
            raise HTTPException(status_code=409, detail="只有待裁决或挂起待证据的草稿可以修改后再议")
        before = requirement_draft_snapshot(requirement)
        normalized_evidence_level = payload.evidence_level if payload.evidence_level in EVIDENCE_LEVELS else "E0"
        normalized_confidence = payload.confidence if payload.kano_candidate != "待分析" else 0
        previous = before.copy()
        requirement.update({
            "title": payload.title.strip(), "subtitle": payload.user_problem.strip(),
            "rationality": payload.rationality_assessment.strip(), "value": payload.value_hypothesis.strip(),
            "evidence": payload.evidence.strip(), "evidenceGap": payload.evidence_gap.strip(),
            "kano": payload.kano_candidate, "why": payload.classification_rationale.strip(),
            "evidenceLevel": normalized_evidence_level, "priority": payload.priority,
            "confidence": normalized_confidence, "aiRecommendationLabel": "AI建议，非结论",
            "state": "待你裁决", "owner": "项目发起人", "humanDecision": "修改后再议",
            "humanDecisionAt": now_iso(), "decisionNote": payload.note.strip(),
            "draftVersion": int(requirement.get("draftVersion", 1)) + 1,
            "lastRevisionActionId": payload.action_id, "analysisUpdatedAt": now_iso(),
        })
        requirement.setdefault("analysisHistory", []).append({
            "previous": previous, "updatedAt": requirement["analysisUpdatedAt"],
            "requestedBy": "user", "note": payload.note.strip(), "new": requirement_draft_snapshot(requirement),
        })
        append_requirement_decision_history(requirement, action="修改后再议", note=payload.note.strip(), actor="项目发起人", before=before)
        summary = f"项目发起人修改 {requirement_id} 并重新提交裁决"
        bump_documents(state, ["doc-1", "doc-2", "doc-9"], summary)
        append_document_record(
            state, ["doc-1", "doc-2", "doc-9"], "## 用户修改后再议记录",
            f"### {requirement_id} · 草稿 v{requirement['draftVersion']}\n\n"
            f"- 用户问题：{requirement['subtitle']}\n- 合理性评估：{requirement['rationality']}\n"
            f"- 价值假设：{requirement['value']}\n- Kano 候选：{requirement['kano']}；依据：{requirement['why']}\n"
            f"- 证据：{requirement['evidence']}（{normalized_evidence_level}）\n- 证据缺口：{requirement['evidenceGap']}\n"
            f"- 修改理由：{payload.note.strip()}\n- 状态：待你裁决；尚未形成正式基线\n- 时间：{requirement['analysisUpdatedAt']}",
        )
        append_chat(state, "group", {
            "from": "项目发起人", "who": "Z", "color": "#bd9773",
            "time": datetime.now().astimezone().strftime("%H:%M"),
            "text": f"{requirement_id} 已由项目发起人修改并重新提交裁决，草稿版本 v{requirement['draftVersion']}。修改理由：{payload.note.strip()}",
        })
        store.save(state, project_id, snapshot_actor="项目发起人")
        store.record_event("requirement.revised", "user", f"修改后再议 {requirement_id} v{requirement['draftVersion']}", project_id)
        return {"state": state, "requirement": requirement, "duplicate": False}


@app.post("/api/requirements/{requirement_id}/clarification")
def request_requirement_clarification(requirement_id: str, payload: RequirementClarificationPayload, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    """Return a pending requirement to analysis and preserve the reason for follow-up."""
    require_project(project_id)
    note = payload.note.strip()
    with STORE_LOCK:
        state = store.load(project_id) or {}
        requirement = next((r for r in state.get("requirements", []) if r.get("id") == requirement_id), None)
        if requirement is None:
            raise HTTPException(status_code=404, detail="Requirement not found")
        if requirement.get("state") not in {"待你裁决", "挂起待证据"}:
            raise HTTPException(status_code=409, detail="只有待裁决或挂起待证据的需求可以退回补充分析")
        requirement["state"] = "分析中"
        requirement["owner"] = ROLE_INFO["pe"]["name"]
        requirement.setdefault("clarificationHistory", []).append({"note": note, "createdAt": now_iso(), "requestedBy": "user"})
        requirement.setdefault("decisionHistory", []).append({
            "action": "退回补充分析", "actor": "项目发起人", "createdAt": now_iso(), "note": note,
            "after": requirement_draft_snapshot(requirement), "draftVersion": int(requirement.get("draftVersion", 1)),
            "baselineVersion": requirement.get("baselineVersion"),
        })
        bump_documents(state, ["doc-1", "doc-2", "doc-9"], f"{requirement_id} 退回补充分析")
        append_document_record(
            state, ["doc-1", "doc-2", "doc-9"], "## 需求补充分析记录",
            f"### {requirement_id} · 退回分析\n\n- 需求：{requirement.get('title', '')}\n- 用户要求补充：{note}\n"
            f"- 责任人：{ROLE_INFO['pe']['name']}\n- 状态：分析中\n- 时间：{now_iso()}",
        )
        append_chat(state, "group", {
            "from": "项目发起人", "who": "Z", "color": "#bd9773", "time": datetime.now().astimezone().strftime("%H:%M"),
            "text": f"{requirement_id} 已退回产品工程师补充分析：{note}",
        })
        store.save(state, project_id, snapshot_actor="user")
        store.record_event("requirement.clarification_requested", "user", f"{requirement_id}：{note[:150]}", project_id)
        return {"state": state, "requirement": requirement}


@app.post("/api/tasks")
@synchronized
def post_task(request: TaskCreateRequest, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    require_project(project_id)
    if request.assignee_role not in ROLE_INFO:
        raise HTTPException(status_code=422, detail="Unknown assignee role")
    state = store.load(project_id) or {}
    requirement = next((r for r in state.get("requirements", []) if r.get("id") == request.requirement_id), None)
    if requirement is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    if requirement.get("state") != "已确认":
        raise HTTPException(status_code=409, detail="先由用户确认需求，再创建研发任务")
    info = ROLE_INFO[request.assignee_role]
    task = {
        "id": next_task_id(state), "title": request.title.strip(), "req": request.requirement_id,
        "state": "待开始", "owner": info["name"], "who": info["short"],
        "color": info["color"], "due": request.due.strip() or "待排期", "acceptanceCriteria": request.acceptance_criteria.strip(),
        "parallelAllowed": request.allow_parallel,
    }
    state.setdefault("tasks", []).insert(0, task)
    record_task_change(state, task, f"新增 {task['id']} · {task['title']}")
    append_chat(state, "group", {
        "from": "项目发起人", "who": "Z", "color": "#bd9773", "time": datetime.now().astimezone().strftime("%H:%M"),
        "text": f"已创建任务 {task['id']}「{task['title']}」，关联 {request.requirement_id}，负责人：{info['name']}。",
    })
    store.save(state, project_id, snapshot_actor="user")
    store.record_event("task.created", "user", f"创建 {task['id']} · {task['title']} · {request.requirement_id}", project_id)
    return {"task": task, "state": state}


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: str, request: TaskUpdateRequest, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    require_project(project_id)
    with STORE_LOCK:
        state = store.load(project_id) or {}
        task = next((item for item in state.get("tasks", []) if item.get("id") == task_id), None)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        previous = str(task.get("state", "待开始"))
        if request.state == "进行中":
            enforce_task_parallelism(state, task)
        apply_task_transition(task, request.state, request.note, "项目发起人")
        summary = f"{task_id}：{previous} → {request.state}"
        record_task_change(state, task, summary)
        append_chat(state, "group", {
            "from": "项目发起人", "who": "Z", "color": "#bd9773", "time": datetime.now().astimezone().strftime("%H:%M"),
            "text": f"{summary}。{request.note.strip()}".rstrip("。"),
        })
        store.save(state, project_id, snapshot_actor="user")
        store.record_event("task.status_changed", "user", f"{summary} · {request.note[:140]}", project_id)
        return {"task": task, "state": state}


@app.patch("/api/tasks/{task_id}/parallelism")
def update_task_parallelism(task_id: str, request: TaskParallelismRequest, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    require_project(project_id)
    with STORE_LOCK:
        state = store.load(project_id) or {}
        task = next((item for item in state.get("tasks", []) if item.get("id") == task_id), None)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        other_active = [item for item in state.get("tasks", []) if item.get("state") == "进行中" and item.get("id") != task_id]
        if not request.allow_parallel and task.get("state") == "进行中" and other_active:
            raise HTTPException(status_code=409, detail="该任务正在与其他任务并行；请先释放其他执行位，再切换为串行")
        task["parallelAllowed"] = request.allow_parallel
        policy = "允许并行" if request.allow_parallel else "设为串行"
        summary = f"{task_id}「{task.get('title', '')}」执行策略调整为{policy}"
        record_task_change(state, task, summary)
        append_chat(state, "group", {
            "from": "项目发起人", "who": "Z", "color": "#bd9773",
            "time": datetime.now().astimezone().strftime("%H:%M"), "text": summary,
        })
        store.save(state, project_id, snapshot_actor="user")
        store.record_event("task.parallelism_changed", "user", summary, project_id)
        return {"task": task, "state": state}


@app.put("/api/team/reporting-lines")
def update_reporting_lines(request: ReportingLinesRequest, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    require_project(project_id)
    expected_roles = set(ROLE_INFO) - {"pdt"}
    if set(request.lines) != expected_roles:
        raise HTTPException(status_code=422, detail="必须为除 PDT 外的每个项目角色设置汇报对象")
    if any(role not in ROLE_INFO or manager not in ROLE_INFO or role == manager or manager == "pdt" and role == "pdt" for role, manager in request.lines.items()):
        raise HTTPException(status_code=422, detail="汇报关系必须引用有效且不同的项目角色")
    for start in expected_roles:
        seen: set[str] = set()
        current = start
        while current != "pdt":
            if current in seen or current not in request.lines:
                raise HTTPException(status_code=422, detail="汇报关系不能形成循环，且最终需要汇报至 PDT")
            seen.add(current)
            current = request.lines[current]
    with STORE_LOCK:
        state = store.load(project_id) or {}
        state["reportingLines"] = request.lines
        store.save(state, project_id, snapshot_actor="user")
        store.record_event("team.reporting_lines_updated", "user", "更新项目角色汇报关系", project_id)
        return {"state": state}


@app.put("/api/team/config")
def update_team_config(request: TeamConfigRequest, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    require_project(project_id)
    known_roles = set(ROLE_INFO)
    if set(request.member_counts) - known_roles or set(request.role_prompts) - known_roles or set(request.role_runtimes) - known_roles:
        raise HTTPException(status_code=422, detail="角色配置包含未知角色")
    if any(type(count) is not int or count < 1 or count > 12 for count in request.member_counts.values()):
        raise HTTPException(status_code=422, detail="每类角色人数必须为 1 至 12 的整数")
    prompts = {role: str(prompt).strip() for role, prompt in request.role_prompts.items()}
    if any(len(prompt) > 4000 for prompt in prompts.values()):
        raise HTTPException(status_code=422, detail="角色提示词最多 4000 个字符")
    runtimes: dict[str, dict[str, str]] = {}
    for role, config in request.role_runtimes.items():
        workspace_path = config.workspace_path.strip()
        if config.provider != "platform":
            if not workspace_path:
                raise HTTPException(status_code=422, detail=f"{ROLE_INFO[role]['name']} 使用子 Runtime 时必须配置工作区绝对路径")
            candidate = Path(workspace_path).expanduser()
            if not candidate.is_absolute():
                raise HTTPException(status_code=422, detail="子 Runtime 工作区必须使用绝对路径")
            try:
                candidate = candidate.resolve(strict=True)
            except OSError as error:
                raise HTTPException(status_code=422, detail="子 Runtime 工作区不存在或不可访问") from error
            if not candidate.is_dir():
                raise HTTPException(status_code=422, detail="子 Runtime 工作区路径必须指向一个目录")
            workspace_path = str(candidate)
        runtimes[role] = {"provider": config.provider, "workspacePath": workspace_path}
    with STORE_LOCK:
        state = store.load(project_id) or {}
        counts = {role: max(1, min(12, int((state.get("memberCounts") or {}).get(role, 1)))) for role in known_roles}
        counts.update(request.member_counts)
        role_prompts = dict(state.get("rolePrompts") or {})
        for role, prompt in prompts.items():
            if prompt:
                role_prompts[role] = prompt
            else:
                role_prompts.pop(role, None)
        role_runtimes = dict(state.get("roleRuntimes") or {})
        role_runtimes.update(runtimes)
        state["memberCounts"] = counts
        state["rolePrompts"] = role_prompts
        state["roleRuntimes"] = role_runtimes
        store.save(state, project_id, snapshot_actor="user")
        store.record_event("team.config_updated", "user", "更新项目角色人数、提示词或 Runtime 配置", project_id)
        return {"state": state}


@app.put("/api/meetings/config")
def update_meeting_config(config: MeetingConfig, project_id: str = Query(default="default", min_length=1, max_length=80)) -> dict[str, Any]:
    require_project(project_id)
    state = store.load(project_id) or {}
    was_enabled = bool(state.get("meetingEnabled"))
    old_cadence = int(state.get("cadence", 5))
    state["cadence"] = config.cadence
    state["meetingEnabled"] = config.enabled
    if config.agenda is not None:
        cleaned_agenda = [str(item).strip()[:300] for item in config.agenda if str(item).strip()][:10]
        state["meetingAgenda"] = cleaned_agenda or [
            "PDT：需要用户裁决的产品问题", "PM：需求、任务、交付物和依赖进度",
            "系统工程师：技术决策与变更影响", "各交付角色：下一步、风险与阻塞",
        ]
    if config.enabled and (not was_enabled or old_cadence != config.cadence):
        state["nextAutoMeetingAt"] = (datetime.now(timezone.utc) + timedelta(minutes=config.cadence)).isoformat(timespec="seconds")
    elif not config.enabled:
        state.pop("nextAutoMeetingAt", None)
    store.save(state, project_id)
    store.record_event("meeting.configured", "user", f"enabled={config.enabled}, cadence={config.cadence}m", project_id)
    return {"enabled": config.enabled, "cadence": config.cadence, "agenda": state.get("meetingAgenda", [])}


@app.post("/api/meetings/run")
async def start_meeting(
    request: MeetingRunRequest | None = None,
    project_id: str = Query(default="default", min_length=1, max_length=80),
) -> dict[str, Any]:
    require_project(project_id)
    request = request or MeetingRunRequest()
    try:
        return await run_project_meeting(project_id=project_id, topic=request.topic, agenda=request.agenda or None)
    except Exception as error:
        fail_active_meeting(project_id, error, "user")
        raise


@app.post("/api/meetings/{meeting_id}/actions/{action_id}/task")
@synchronized
def promote_meeting_action_to_task(
    meeting_id: str, action_id: str, project_id: str = Query(default="default", min_length=1, max_length=80),
) -> dict[str, Any]:
    require_project(project_id)
    state = store.load(project_id) or {}
    meeting = next((item for item in state.get("meetings", []) if item.get("id") == meeting_id), None)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    action = next((item for item in meeting.get("actionItems", []) if item.get("id") == action_id), None)
    if action is None:
        raise HTTPException(status_code=404, detail="Meeting action item not found")
    if action.get("taskId"):
        existing = next((item for item in state.get("tasks", []) if item.get("id") == action["taskId"]), None)
        if existing:
            return {"task": existing, "action": action, "state": state, "idempotent": True}
        raise HTTPException(status_code=409, detail="行动项已关联的任务当前不存在，请先恢复或修复该任务")
    requirement_id = str(action.get("requirementId", ""))
    requirement = next((item for item in state.get("requirements", []) if item.get("id") == requirement_id), None)
    if requirement is None or requirement.get("state") != "已确认":
        raise HTTPException(status_code=409, detail="行动项必须关联一条已确认的需求")
    assignee_role = str(action.get("assigneeRole", ""))
    if assignee_role not in ROLE_INFO or assignee_role == "pdt":
        raise HTTPException(status_code=409, detail="行动项需要指定有效的交付角色")
    title = str(action.get("title", "")).strip()
    criteria = str(action.get("acceptanceCriteria", "")).strip()
    if len(title) < 3 or len(criteria) < 3:
        raise HTTPException(status_code=409, detail="行动项需要标题和可验证的验收标准")
    info = ROLE_INFO[assignee_role]
    task = {
        "id": next_task_id(state), "title": title, "req": requirement_id, "state": "待开始",
        "owner": info["name"], "who": info["short"], "color": info["color"],
        "due": str(action.get("due", "待排期"))[:40] or "待排期", "acceptanceCriteria": criteria,
        "meetingId": meeting_id, "meetingActionId": action_id,
    }
    state.setdefault("tasks", []).insert(0, task)
    action.update({"taskId": task["id"], "status": "已转为任务", "eligible": True, "confirmedAt": now_iso(), "confirmedBy": "user"})
    record_task_change(state, task, f"会议 {meeting_id} 行动项 {action_id} 经用户确认并转为正式任务")
    promotion_summary = f"会议 {meeting_id} 行动项 {action_id} 经用户确认并转为任务 {task['id']}"
    meeting_doc_ids = ["doc-2", "doc-4", "doc-5", "doc-6", "doc-9"]
    for document in state.get("docs", []):
        if document.get("id") not in meeting_doc_ids:
            continue
        lines = str(document.get("content") or "").splitlines()
        current_meeting_id = ""
        changed = False
        for index, line in enumerate(lines):
            meeting_heading = re.match(r"###\s+(MEET-\d+)\s+·", line)
            if meeting_heading:
                current_meeting_id = meeting_heading.group(1)
            elif line.startswith("## "):
                current_meeting_id = ""
            if current_meeting_id == meeting_id and action_id in line and "待确认" in line:
                lines[index] = line.replace("· 待确认", f"· 已转为任务 {task['id']}", 1)
                changed = True
        if changed:
            document["content"] = "\n".join(lines).rstrip() + "\n"
    append_document_record(
        state, meeting_doc_ids, "## 会议行动项确认记录",
        f"### {action_id} · 已转为正式任务\n\n- 会议：{meeting_id}\n- 任务：{task['id']} · {title}\n"
        f"- 关联需求：{requirement_id}\n- 负责人：{info['name']}\n- 验收标准：{criteria}\n- 确认人：项目发起人\n- 变更：{promotion_summary}",
    )
    bump_documents(state, ["doc-2"], promotion_summary)
    append_chat(state, "group", {
        "from": "项目发起人", "who": "Z", "color": "#bd9773", "time": datetime.now().astimezone().strftime("%H:%M"),
        "text": f"已确认会议行动项 {action_id} 并创建任务 {task['id']}「{title}」，关联 {requirement_id}，负责人：{info['name']}。",
    })
    store.save(state, project_id, snapshot_actor="user")
    store.record_event("meeting.action_promoted", "user", f"{meeting_id} / {action_id} → {task['id']} · {requirement_id}", project_id)
    return {"task": task, "action": action, "state": state, "idempotent": False}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "index.html")


@app.get("/app.js")
def javascript() -> FileResponse:
    return FileResponse(ROOT / "app.js", media_type="text/javascript")


@app.get("/styles.css")
def stylesheet() -> FileResponse:
    return FileResponse(ROOT / "styles.css", media_type="text/css")
