# DevTing — From product intent to a roadmap teams can follow

<p align="center">
  <a href="README.md">English</a> · <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="assets/devting-overview.svg" alt="DevTing turns a product idea into human-approved requirements, coordinated work, and traceable delivery." width="100%" />
</p>

<p align="center">
  <img alt="Development status" src="https://img.shields.io/badge/status-development-8b7cf6?style=flat-square" />
  <img alt="Python 3.12 and newer" src="https://img.shields.io/badge/python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white" />
  <img alt="AI orchestration with Deep Agents" src="https://img.shields.io/badge/AI-Deep%20Agents-6558e8?style=flat-square" />
  <img alt="Local-first MVP" src="https://img.shields.io/badge/deployment-local%20MVP-27a785?style=flat-square" />
</p>

> **Development-stage local MVP.** DevTing explores a more deliberate way to build software: make intent clear, keep people in control of decisions, and preserve the reasoning and progress that usually disappear inside long conversations. It is still evolving and is not production-ready or a multi-user service.

## Why DevTing

The slowest part of software work is often deciding what to build, why it matters, and what “done” means. DevTing brings that work into the development loop. AI helps clarify the problem and prepare recommendations; **you approve the scope**; the project team then turns approved requirements into connected design, implementation, and test work.

The roadmap, role conversations, decisions, meeting notes, and document versions stay in the local project space, so delivery can continue without relying on one ever-growing chat history.

## What you can do

- **Start a project with context** — capture the goal, product shape, initial problem, and waterfall, agile, or hybrid delivery mode. New projects begin with a guided setup and a clean requirements baseline.
- **Analyze requirements with a human decision gate** — PDT and product roles surface value hypotheses, evidence gaps, and Kano category suggestions. Approve, revise, reject, or defer each proposal; only approved requirements enter the baseline.
- **Turn approved scope into coordinated work** — PM creates linked role tasks and acceptance criteria. Track UI, engineering, and test work from requirement through review and completion.
- **Explore a live, tree-shaped roadmap** — expand delivery stages into requirements, tasks, and deliverables. Inspect node status and project events, and set serial or parallel execution with a server-enforced project concurrency limit.
- **Work with a configurable AI team** — use 11 role types across PDT, PM, systems, product, UI, frontend, backend, data, algorithms, testing, and operations. Configure role count, prompt, and reporting relationships.
- **Choose a runtime per role** — route work through the platform’s Deep Agents runtime, Codex CLI, or Claude Code CLI. External CLIs run on the service machine against a configured local repository; they do not receive the platform’s model key.
- **Keep project memory and deliverables together** — maintain the product requirements package, PRD, high- and low-level design, test and self-test plans, operations notes, decisions, and versioned project documents.
- **Review meetings and decisions before they change the plan** — run meetings manually or enable a local schedule. AI action proposals require your confirmation before tasks are created.
- **Read and export the project clearly** — Markdown is rendered across chat and project views. Export requirements as CSV, a delivery summary as Markdown, or the project history and document versions as JSON.

Kano labels are recommendations with rationale and evidence gaps, not validated market research. The roadmap shows agent steps and persisted project events; it is not token-level streaming or a distributed multi-agent scheduler.

## A project flow with human approval at the center

1. **Create a project** and describe the problem, audience, product shape, and delivery mode.
2. **Clarify with the PDT manager.** Separate evidence from assumptions and identify unanswered questions.
3. **Review requirement proposals.** Check the suggested Kano category, reasoning, confidence, and missing evidence. An attractive requirement may delight users when present without causing equal dissatisfaction when absent.
4. **Make the scope decision.** Approve, revise, reject, or defer. Approval establishes the baseline and queues PM decomposition; the other outcomes do not create delivery work.
5. **Follow work on the roadmap.** Expand a stage to see its requirements, role tasks, dependencies, deliverables, and event history. Adjust concurrency and serial/parallel execution.
6. **Review work and artifacts.** Track acceptance criteria, tests, design documents, decisions, and meeting actions. AI recommendations remain reviewable until you accept them.

## Quick start

Requires Python 3.12 or newer. First install the dependencies and create a local environment:

```bash
git clone https://github.com/ZXTFINAL/DevTing.git
cd DevTing
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Add your own DeepSeek API key to `DEEPSEEK_API_KEY` in `.env`, then run the app:

```bash
uvicorn backend.app:app --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765/> and create a project. Without a model key, you can still inspect and edit local project data and documents, but AI chat, requirement analysis, automatic PM decomposition, and meetings are unavailable. Keep provider keys in the server-side `.env`; never commit them or paste them into browser code.

### Connect a coding runtime to a role

In **Team**, open a role’s settings and choose Platform Runtime, Codex CLI, or Claude Code. For a CLI, set the absolute path to a local code repository. The CLI must be installed and authenticated on the machine running DevTing. The “command available” indicator only detects the executable; login is checked when the runtime is called.

An external runtime can inspect or edit its configured code repository, but currently has no direct DevTing database tools. Its report is synced to the role conversation and project group; the platform role is responsible for persisting any structured project updates. The platform runtime retains the project requirement, task, and document tools.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API key. Required for real model calls. |
| `DEEPSEEK_BASE_URL` | OpenAI-compatible API endpoint; defaults to `https://api.deepseek.com`. |
| `DEEPAGENTS_MODEL` | Deep Agents model identifier; defaults to `openai:deepseek-flash`. |
| `DEVTING_DB_PATH` | Optional SQLite path override; defaults to `.data/zhidian.sqlite3`. |

By default, the server listens only on `127.0.0.1`. Do not expose it publicly without authentication, access controls, backups, and a data-retention policy. Scheduled meetings are off by default and only run while the local service is running.

## Architecture

```text
Browser (HTML / CSS / JavaScript)
              │ HTTP / JSON
FastAPI ──────┼── Deep Agents + OpenAI-compatible model
              └── SQLite (project state, audit events, document versions)
```

- `backend/app.py` — API, project state, SQLite persistence, Deep Agents orchestration, and scheduling.
- `app.js`, `styles.css`, `index.html` — single-page workspace and interactions.
- `docs/` — product vision, requirements, PRD, software design, test plans, operations, and decision records.

## Run the isolated integration checks

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

The checks use a temporary SQLite database and deterministic role doubles. They do not call a live model or read/write `.data/zhidian.sqlite3`. Coverage includes project creation, requirement approval gates, PM decomposition, task review and acceptance, meeting actions, memory review, document versions, exports, project isolation, and per-role runtime routing with a fake CLI. Passing tests does not verify real-model output quality, the full browser journey, or vendor CLI login/output compatibility. See the [closed-loop test report](docs/test-report-2026-10-13-closed-loop.md).

## Current limits

This is a local, single-user MVP. Important work remains around full browser-flow acceptance, usability across projects and devices, real-model quality and cost controls, meeting interruption recovery, data import, backup/restore drills, multi-user access control, and concurrent writes. Delivery modes currently share tracking and stage guidance but do not enforce every phase gate. The reporting hierarchy supports agent context and project records; it is not a full real-time team messenger.

For more detail, see the [test report](docs/test-report-2026-10-09.md), [test outline](docs/test-outline.md), and the product and design documents below.

## Product and design documents

- [Product vision and founder notes](docs/product-vision.md)
- [Product requirements package](docs/product-requirements-package.md)
- [Product requirements document (PRD)](docs/PRD.md)
- [Software high-level design](docs/software-high-level-design.md)
- [Software detailed design](docs/software-detailed-design.md)
- [Test outline](docs/test-outline.md) · [Self-test outline](docs/self-test-outline.md)
- [Roadmap verification](docs/test-report-2026-10-13-roadmap.md)
- [Markdown and role runtime verification](docs/test-report-2026-10-13-markdown-runtime.md)
- [Closed-loop integration report](docs/test-report-2026-10-13-closed-loop.md)
- [Operations outline](docs/operations-outline.md) · [Decisions and open questions](docs/decisions-and-open-questions.md)

## License

No license has been selected yet. Until one is added, do not assume the code can be reused, modified, or redistributed.
