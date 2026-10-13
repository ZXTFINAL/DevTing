"""Isolated integration checks for the local MVP's core project loop.

Run with: python -m unittest discover -s tests -v
The tests use a temporary SQLite database, deterministic role stubs, and fake
local CLI executables. They never call a real model/vendor CLI or read/write the
user's project database.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from fastapi.testclient import TestClient


TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="devting-mvp-test-"))
os.environ["DEVTING_DB_PATH"] = str(TEST_DATA_DIR / "isolated.sqlite3")
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""

devting = importlib.import_module("backend.app")


class MvpClosedLoopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(devting.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

    def test_runtime_choice_launches_fake_child_clis_and_syncs_reports(self) -> None:
        created = self.client.post(
            "/api/projects",
            json={"name": "角色 Runtime 路由隔离验收", "description": "验证每个角色可以独立选择执行 Runtime。"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        project_id = created.json()["project"]["project_id"]
        workspace_fe = TEST_DATA_DIR / "workspace-fe"
        workspace_qa = TEST_DATA_DIR / "workspace-qa"
        workspace_fe.mkdir()
        workspace_qa.mkdir()

        configured = self.client.put(
            f"/api/team/config?project_id={project_id}",
            json={
                "member_counts": {},
                "role_prompts": {"fe": "只负责前端代码任务。"},
                "role_runtimes": {
                    "pdt": {"provider": "platform", "workspace_path": ""},
                    "fe": {"provider": "codex", "workspace_path": str(workspace_fe)},
                    "qa": {"provider": "claude_code", "workspace_path": str(workspace_qa)},
                },
            },
        )
        self.assertEqual(configured.status_code, 200, configured.text)
        saved = self.client.get(f"/api/state?project_id={project_id}").json()
        self.assertEqual(saved["roleRuntimes"]["pdt"]["provider"], "platform")
        self.assertEqual(saved["roleRuntimes"]["fe"]["provider"], "codex")
        self.assertEqual(saved["roleRuntimes"]["qa"]["provider"], "claude_code")
        self.assertEqual(saved["rolePrompts"]["fe"], "只负责前端代码任务。")

        invalid_workspace = self.client.put(
            f"/api/team/config?project_id={project_id}",
            json={"role_runtimes": {"be": {"provider": "codex", "workspace_path": "relative/path"}}},
        )
        self.assertEqual(invalid_workspace.status_code, 422)

        fake_bin = TEST_DATA_DIR / "fake-bin"
        fake_bin.mkdir()
        fake_cli = fake_bin / "fake-runtime-template"
        fake_cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "name = pathlib.Path(sys.argv[0]).name\n"
            "prompt = sys.stdin.read()\n"
            "log_name = 'FAKE_CODEX_LOG' if name == 'codex' else 'FAKE_CLAUDE_LOG'\n"
            "with open(os.environ[log_name], 'w', encoding='utf-8') as stream:\n"
            "    json.dump({'args': sys.argv[1:], 'cwd': os.getcwd(), 'prompt': prompt, "
            "'deepseek_key_present': 'DEEPSEEK_API_KEY' in os.environ, "
            "'openai_key_present': 'OPENAI_API_KEY' in os.environ, "
            "'openai_base_url': os.environ.get('OPENAI_BASE_URL')}, stream)\n"
            "if name == 'codex':\n"
            "    print(json.dumps({'type': 'item.started', 'item': {'type': 'command_execution'}}))\n"
            "    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Codex 子 Runtime 合成报告'}}))\n"
            "else:\n"
            "    print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Claude Code 子 Runtime 合成报告'}]}}))\n",
            encoding="utf-8",
        )
        for cli_name in ("codex", "claude"):
            cli_path = fake_bin / cli_name
            shutil.copyfile(fake_cli, cli_path)
            cli_path.chmod(0o755)

        old_path = os.environ.get("PATH", "")
        with patch.dict(
            os.environ,
            {
                "PATH": f"{fake_bin}{os.pathsep}{old_path}",
                "DEEPSEEK_API_KEY": "synthetic-deepseek-secret",
                "OPENAI_API_KEY": "synthetic-deepseek-secret",
                "OPENAI_BASE_URL": "https://api.deepseek.com/v1",
                "FAKE_CODEX_LOG": str(TEST_DATA_DIR / "codex-invocation.json"),
                "FAKE_CLAUDE_LOG": str(TEST_DATA_DIR / "claude-invocation.json"),
            },
        ):
            import asyncio

            codex_chat = self.client.post(
                "/api/chat",
                json={"role": "fe", "message": "实现一个合成界面任务。", "project_id": project_id},
            )
            self.assertEqual(codex_chat.status_code, 200, codex_chat.text)
            self.assertEqual(codex_chat.json()["reply"], "Codex 子 Runtime 合成报告")
            claude_chat = self.client.post(
                "/api/chat",
                json={"role": "qa", "message": "检查一个合成验收场景。", "project_id": project_id},
            )
            self.assertEqual(claude_chat.status_code, 200, claude_chat.text)
            self.assertEqual(claude_chat.json()["reply"], "Claude Code 子 Runtime 合成报告")

            missing_bin = TEST_DATA_DIR / "missing-bin"
            missing_bin.mkdir()
            with patch.dict(os.environ, {"PATH": str(missing_bin)}):
                unavailable = self.client.post(
                    "/api/chat",
                    json={"role": "fe", "message": "验证 CLI 缺失时的错误。", "project_id": project_id},
                )
            self.assertEqual(unavailable.status_code, 502)
            self.assertIn("没有找到 Codex CLI", unavailable.json()["detail"])

            # The emitted answer must reach both the role conversation and the project group.
            final_state = self.client.get(f"/api/state?project_id={project_id}").json()
            self.assertTrue(any("Codex 子 Runtime 合成报告" in item.get("text", "") for item in final_state["chats"]["fe"]))
            self.assertTrue(any("Codex 子 Runtime 合成报告" in item.get("text", "") for item in final_state["chats"]["group"]))
            self.assertTrue(any("Claude Code 子 Runtime 合成报告" in item.get("text", "") for item in final_state["chats"]["qa"]))
            self.assertTrue(any("Claude Code 子 Runtime 合成报告" in item.get("text", "") for item in final_state["chats"]["group"]))

        codex_log = json.loads((TEST_DATA_DIR / "codex-invocation.json").read_text(encoding="utf-8"))
        self.assertEqual(codex_log["cwd"], str(workspace_fe.resolve()))
        self.assertIn("--sandbox", codex_log["args"])
        self.assertIn("workspace-write", codex_log["args"])
        self.assertIn("只负责前端代码任务。", codex_log["prompt"])
        self.assertIn("实现一个合成界面任务。", codex_log["prompt"])
        self.assertFalse(codex_log["deepseek_key_present"])
        self.assertFalse(codex_log["openai_key_present"])
        self.assertIsNone(codex_log["openai_base_url"])

        claude_log = json.loads((TEST_DATA_DIR / "claude-invocation.json").read_text(encoding="utf-8"))
        self.assertEqual(claude_log["cwd"], str(workspace_qa.resolve()))
        self.assertIn("acceptEdits", claude_log["args"])
        self.assertIn("检查一个合成验收场景。", claude_log["prompt"])
        self.assertFalse(claude_log["deepseek_key_present"])
        self.assertFalse(claude_log["openai_key_present"])

        events = self.client.get(f"/api/events?project_id={project_id}&limit=100").json()
        self.assertTrue(any(event["event_type"] == "agent.progress" and "Codex 子 Runtime" in event["summary"] for event in events))
        self.assertTrue(any(event["event_type"] == "agent.reply" for event in events))
        self.assertTrue(any(event["event_type"] == "agent.run_failed" for event in events))

    def test_startup_recovers_interrupted_meeting_and_is_idempotent(self) -> None:
        created = self.client.post(
            "/api/projects",
            json={"name": "会议进程恢复隔离验收", "description": "验证服务重启后的会议运行标记恢复。"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        project_id = created.json()["project"]["project_id"]
        state = devting.store.load(project_id)
        state["meetingEnabled"] = True
        state["meetingLive"] = True
        state["cadence"] = 5
        state["nextAutoMeetingAt"] = "2020-01-01T00:00:00+00:00"
        state["meetings"] = [
            {
                "id": "MEET-001",
                "title": "重启前会议",
                "state": "进行中",
                "transcript": [{"from": "PDT 经理", "text": "已保存的部分发言。"}],
            }
        ]
        devting.store.save(state, project_id)

        with TestClient(devting.app) as startup_client:
            recovered = startup_client.get(f"/api/state?project_id={project_id}").json()
        self.assertFalse(recovered["meetingLive"])
        self.assertEqual(recovered["meetings"][0]["state"], "失败")
        self.assertEqual(len(recovered["meetings"][0]["transcript"]), 1)
        self.assertIn("服务进程已停止", recovered["meetings"][0]["errors"][0])
        next_auto = datetime.fromisoformat(recovered["nextAutoMeetingAt"])
        self.assertGreater(next_auto, datetime.now(timezone.utc))
        first_failure_count = sum(
            event["event_type"] == "meeting.failed"
            for event in devting.store.events(project_id, limit=100)
        )
        self.assertEqual(first_failure_count, 1)

        devting.recover_interrupted_meetings()
        self.assertEqual(
            sum(event["event_type"] == "meeting.failed" for event in devting.store.events(project_id, limit=100)),
            first_failure_count,
        )

    def test_project_requirement_approval_pm_delivery_meeting_memory_and_export(self) -> None:
        runtime = {
            "agent_configured": True,
            "model": "test:deterministic-stub",
            "missing": [],
            "provider": "platform",
        }
        created = self.client.post(
            "/api/projects",
            json={
                "name": "MVP 闭环隔离验收",
                "description": "# 初始想法\n\n用 HITL 管理项目范围。",
                "product_shape": "Web 应用",
                "development_mode": "敏捷迭代",
            },
        )
        self.assertEqual(created.status_code, 200)
        project = created.json()["project"]
        project_id = project["project_id"]
        initial_state = created.json()["state"]
        self.assertEqual(initial_state["requirements"], [])
        self.assertEqual(initial_state["tasks"], [])
        self.assertEqual(len(initial_state["docs"]), 9)

        other_project = self.client.post(
            "/api/projects",
            json={"name": "另一个隔离项目", "description": "不应混入第一项目数据。"},
        ).json()["project"]["project_id"]

        def invoke_role(project_arg: str, role_id: str, prompt: str, history: Any = None) -> str:
            del history
            tools = {tool.name: tool for tool in devting.project_tools(project_arg, role_id)}
            if role_id == "pdt" and prompt == user_prompt:
                result = tools["create_requirement"].invoke(
                    {
                        "title": "项目方向与需求证据可追溯",
                        "user_problem": "项目初期需求来源和证据容易散落在会话里。",
                        "rationality_assessment": "该问题会影响范围判断与后续验收，值得验证。",
                        "kano_candidate": "期望型",
                        "classification_rationale": "证据不足以证明惊喜属性，先作为期望型候选。",
                        "value_hypothesis": "把来源和待验证项显示在项目工作区能减少重复澄清。",
                        "evidence": "当前仅有项目发起人的初始描述。",
                        "evidence_gap": "尚无目标用户访谈或使用数据。",
                        "priority": "P1",
                        "confidence": 42,
                        "evidence_level": "E0",
                    }
                )
                self.assertTrue(result["human_approval_required"])
                return "已整理需求建议，证据等级为 E0，等待你裁决。"
            if role_id == "pm" and prompt.startswith("系统已把需求"):
                requirement = devting.store.load(project_arg)["requirements"][0]
                requirement_id = requirement["id"]
                for title, role in (
                    ("实现需求追溯页面", "fe"),
                    ("验证需求裁决与追溯接口", "qa"),
                ):
                    result = tools["create_development_task"].invoke(
                        {
                            "requirement_id": requirement_id,
                            "title": title,
                            "assignee_role": role,
                            "acceptance_criteria": "提交合成项目后能查到关联需求、证据来源和任务状态。",
                        }
                    )
                    self.assertNotIn("error", result)
                for doc_id in ("doc-2", "doc-6"):
                    result = tools["update_project_document"].invoke(
                        {
                            "document_id": doc_id,
                            "change_summary": f"纳入 {requirement_id} 的范围和验收依据。",
                            "content": f"# 项目交付说明\n\n关联需求：{requirement_id}\n\n- **状态**：等待评审\n",
                        }
                    )
                    self.assertNotIn("error", result)
                tools["send_project_update"].invoke(
                    {"message": f"PM 已为 {requirement_id} 拆解 FE 与 QA 任务，并更新 PRD/自测大纲。"}
                )
                return f"{requirement_id} 拆解完成：2 项角色任务，PRD 与自测大纲已更新。"
            if role_id == "pm" and prompt.startswith("作为项目经理主持并收束会议"):
                state = devting.store.load(project_arg)
                requirement_id = state["requirements"][0]["id"]
                return json.dumps(
                    {
                        "summary": "已核对已批准范围、当前交付任务和证据缺口。",
                        "decisionRequests": ["是否需要补充外部用户样本？"],
                        "deliveryOrder": ["FE 实现追溯视图", "QA 验证裁决与引用链"],
                        "actionItems": [
                            {
                                "title": "补充需求追溯视图的可用性检查",
                                "assigneeRole": "fe",
                                "requirementId": requirement_id,
                                "acceptanceCriteria": "检查来源等级、裁决记录和关联任务均可查看。",
                                "due": "待排期",
                                "source": "会议交付顺序和项目验收标准",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            return "已汇报当前负责范围、下一步和风险；没有自行批准需求或创建会议任务。"

        user_prompt = "请分析需求：让项目内的重要需求、证据和交付状态可以追溯。"
        with patch.object(devting, "runtime_status", return_value=runtime), patch.object(
            devting, "invoke_role", side_effect=invoke_role
        ):
            chat_response = self.client.post(
                "/api/chat",
                json={"role": "pdt", "message": user_prompt, "project_id": project_id},
            )
            self.assertEqual(chat_response.status_code, 200, chat_response.text)
            after_chat = self.client.get(f"/api/state?project_id={project_id}").json()
            draft = after_chat["requirements"][0]
            self.assertTrue(draft["id"].startswith("DRAFT-"))
            self.assertEqual(draft["state"], "待你裁决")
            self.assertEqual(after_chat["tasks"], [])
            self.assertTrue(after_chat["chats"]["group"])

            approval = self.client.post(
                f"/api/requirements/{draft['id']}/approve?project_id={project_id}",
                json={"note": "同意先以 E0 假设建立小范围基线并验证。"},
            )
            self.assertEqual(approval.status_code, 200, approval.text)
            requirement_id = approval.json()["requirement"]["id"]
            self.assertTrue(requirement_id.startswith("REQ-"))
            self.assertEqual(approval.json()["state"]["requirementPlans"][requirement_id]["status"], "queued")
            self.assertEqual(
                len([task for task in approval.json()["state"]["tasks"] if not task.get("autoPlan")]), 0
            )

            import asyncio

            asyncio.run(devting.run_requirement_plan(project_id, requirement_id))
            planned = self.client.get(f"/api/state?project_id={project_id}").json()
            self.assertEqual(planned["requirementPlans"][requirement_id]["status"], "completed")
            children = [task for task in planned["tasks"] if task.get("req") == requirement_id and not task.get("autoPlan")]
            self.assertEqual(len(children), 2)
            self.assertTrue(all(task.get("acceptanceCriteria") for task in children))
            self.assertTrue(any("自动拆解完成" in item.get("text", "") for item in planned["chats"]["group"]))

            fe_task = next(task for task in children if task["who"] == "FE")
            started = self.client.patch(
                f"/api/tasks/{fe_task['id']}?project_id={project_id}",
                json={"state": "进行中", "note": "开始实现追溯页面。"},
            )
            self.assertEqual(started.status_code, 200, started.text)
            blocked_completion = self.client.patch(
                f"/api/tasks/{fe_task['id']}?project_id={project_id}",
                json={"state": "已完成", "note": "跳过评审"},
            )
            self.assertEqual(blocked_completion.status_code, 409)
            finished = self.client.patch(
                f"/api/tasks/{fe_task['id']}?project_id={project_id}",
                json={"state": "评审中", "note": "实现和自测完成，提交 QA 评审。"},
            )
            self.assertEqual(finished.status_code, 200, finished.text)
            missing_acceptance = self.client.patch(
                f"/api/tasks/{fe_task['id']}?project_id={project_id}",
                json={"state": "已完成", "note": ""},
            )
            self.assertEqual(missing_acceptance.status_code, 422)
            finished = self.client.patch(
                f"/api/tasks/{fe_task['id']}?project_id={project_id}",
                json={"state": "已完成", "note": "追溯字段展示通过合成项目验收。"},
            )
            self.assertEqual(finished.status_code, 200, finished.text)

            doc2_before_meeting = next(doc for doc in planned["docs"] if doc["id"] == "doc-2")["version"]
            meeting_response = self.client.post(
                f"/api/meetings/run?project_id={project_id}",
                json={"topic": "追溯能力进度与下一步", "agenda": ["FE/QA 进度", "证据缺口"]},
            )
            self.assertEqual(meeting_response.status_code, 200, meeting_response.text)
            meeting = meeting_response.json()["meeting"]
            self.assertEqual(meeting["state"], "纪要已生成")
            self.assertTrue(meeting["transcript"])
            action = meeting["actionItems"][0]
            self.assertTrue(action["eligible"])
            self.assertFalse(action.get("taskId"))
            meeting_state = meeting_response.json()["state"]
            meeting_doc2 = next(doc for doc in meeting_state["docs"] if doc["id"] == "doc-2")
            self.assertNotEqual(meeting_doc2["version"], doc2_before_meeting)
            self.assertIn(f"{action['id']} {action['title']}", meeting_doc2["content"])
            self.assertIn("待确认", meeting_doc2["content"])
            promoted = self.client.post(
                f"/api/meetings/{meeting['id']}/actions/{action['id']}/task?project_id={project_id}"
            )
            self.assertEqual(promoted.status_code, 200, promoted.text)
            promoted_state = promoted.json()["state"]
            promoted_task = promoted.json()["task"]
            expected_status = f"{action['id']} {action['title']} · {action['assigneeRole']} · {requirement_id} · 已转为任务 {promoted_task['id']}"
            for document_id in ("doc-2", "doc-4", "doc-5", "doc-6", "doc-9"):
                document = next(doc for doc in promoted_state["docs"] if doc["id"] == document_id)
                self.assertIn(expected_status, document["content"])
                self.assertIn(f"{action['id']} · 已转为正式任务", document["content"])
            replay = self.client.post(
                f"/api/meetings/{meeting['id']}/actions/{action['id']}/task?project_id={project_id}"
            )
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertTrue(replay.json()["idempotent"])

            second_meeting_response = self.client.post(
                f"/api/meetings/run?project_id={project_id}",
                json={"topic": "再次核对行动项留痕", "agenda": ["查看新任务", "确认历史行动项"]},
            )
            self.assertEqual(second_meeting_response.status_code, 200, second_meeting_response.text)
            second_meeting = second_meeting_response.json()["meeting"]
            second_action = second_meeting["actionItems"][0]
            self.assertEqual(second_action["id"], action["id"])
            second_promoted = self.client.post(
                f"/api/meetings/{second_meeting['id']}/actions/{second_action['id']}/task?project_id={project_id}"
            )
            self.assertEqual(second_promoted.status_code, 200, second_promoted.text)
            second_task_id = second_promoted.json()["task"]["id"]
            final_doc2 = next(doc for doc in second_promoted.json()["state"]["docs"] if doc["id"] == "doc-2")["content"]
            first_meeting_entry = final_doc2.split(f"### {meeting['id']} ·", 1)[1].split(f"### {second_meeting['id']} ·", 1)[0]
            second_meeting_entry = final_doc2.split(f"### {second_meeting['id']} ·", 1)[1].split("## 会议行动项确认记录", 1)[0]
            self.assertIn(f"{action['id']} {action['title']} · {action['assigneeRole']} · {requirement_id} · 已转为任务 {promoted_task['id']}", first_meeting_entry)
            self.assertIn(f"{second_action['id']} {second_action['title']} · {second_action['assigneeRole']} · {requirement_id} · 已转为任务 {second_task_id}", second_meeting_entry)
            self.assertNotIn(second_task_id, first_meeting_entry)

            fact_response = self.client.post(
                f"/api/facts?project_id={project_id}",
                json={
                    "kind": "假设",
                    "title": "追溯视图减少重复澄清",
                    "content": "如果证据和变更历史能在需求旁查看，用户可能更少重复说明背景。",
                    "source": "本次项目发起人的待验证产品假设。",
                    "evidence_level": "E0",
                    "requirement_id": requirement_id,
                },
            )
            self.assertEqual(fact_response.status_code, 200, fact_response.text)
            fact = fact_response.json()["fact"]
            self.assertEqual(fact["status"], "用户提供")
            user_fact_review = self.client.patch(
                f"/api/facts/{fact['id']}?project_id={project_id}",
                json={"decision": "用户确认", "note": "用户提供内容无需 AI 事实审阅。"},
            )
            self.assertEqual(user_fact_review.status_code, 409)

            memory_tool = next(
                tool for tool in devting.project_tools(project_id, "pm") if tool.name == "record_project_fact"
            )
            ai_fact_result = memory_tool.invoke(
                {
                    "kind": "假设",
                    "title": "追溯视图可能减少重复澄清",
                    "content": "如果来源和变更历史能在需求旁查看，用户可能更少重复说明背景。",
                    "source": f"PM 根据 {requirement_id} 的初始构想提出，尚无用户验证。",
                    "confidence": 30,
                    "requirement_id": requirement_id,
                    "evidence_level": "E0",
                }
            )
            self.assertTrue(ai_fact_result["human_review_required"])
            ai_fact = ai_fact_result["fact"]
            self.assertEqual(ai_fact["status"], "待核实")
            review = self.client.patch(
                f"/api/facts/{ai_fact['id']}?project_id={project_id}",
                json={"decision": "用户确认", "note": "保留为项目上下文，仍标记 E0。"},
            )
            self.assertEqual(review.status_code, 200, review.text)

        persisted = self.client.get(f"/api/state?project_id={project_id}").json()
        self.assertFalse(persisted["meetingLive"])
        reviewed_fact = next(item for item in persisted["facts"] if item["id"] == ai_fact["id"])
        self.assertEqual(reviewed_fact["status"], "用户确认")
        self.assertGreaterEqual(persisted["docs"][1]["progress"], 0)
        versions = self.client.get(f"/api/documents/doc-2/versions?project_id={project_id}")
        self.assertEqual(versions.status_code, 200, versions.text)
        self.assertGreaterEqual(len(versions.json()), 1)
        exported = self.client.get(f"/api/projects/{project_id}/export")
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(exported.json()["project"]["project_id"], project_id)
        self.assertTrue(exported.json()["events"])
        self.assertTrue(exported.json()["documentVersions"])

        isolated_state = self.client.get(f"/api/state?project_id={other_project}").json()
        self.assertEqual(isolated_state["requirements"], [])
        self.assertEqual(isolated_state["tasks"], [])
        self.assertEqual(isolated_state["facts"], [])


if __name__ == "__main__":
    unittest.main()
