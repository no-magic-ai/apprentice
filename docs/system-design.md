# apprentice — Multi-Agent Algorithm Factory for no-magic

> A multi-agent system that implements, instruments, visualizes, tests, and ships new algorithm entries for the no-magic ecosystem. Specialist agents coordinate under an orchestrator to produce complete educational content — from algorithm selection through PR submission.

**Repository**: `no-magic-ai/apprentice`
**Parent ecosystem**: `no-magic-ai/no-magic`

---

## 1. Naming Rationale

`apprentice` — a learner that produces work under supervision, gradually earning autonomy. Maps directly to the v1→v2 trajectory: assisted apprentice → autonomous apprentice with guardrails.

---

## 2. Problem Statement

no-magic currently has 41 algorithms across four tiers, each with:

- Single-file, zero-dependency Python implementation
- Manim animation for visual explanation
- Anki flashcard deck
- Learning track placement
- README documentation

Every new algorithm requires manually producing all five artifacts, maintaining consistency with existing conventions, and validating correctness. This is the bottleneck to catalog growth.

**apprentice** automates the full artifact pipeline using a multi-agent system where specialist agents handle implementation, visualization, assessment, and review — coordinated by an orchestrator that manages budget, sequencing, and quality enforcement.

---

## 3. Design Principles

| Principle                   | Implication                                                                                                                                                                                                                                                          |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Multi-agent by design**   | Each specialist agent has a distinct role, system prompt, tool access, and reasoning loop. Agent boundaries correspond to intuitive roles, not arbitrary splits.                                                                                                     |
| **Honest agent boundaries** | Full agents (orchestrator, discovery, implementation, review) have goal-directed reasoning with self-correction. Tool-agents (instrumentation, visualization, assessment) are thin wrappers — making them full agents would add ceremony without behavioral benefit. |
| **Containment first**       | Autonomous mode has hard budget caps, rate limits, and mandatory human checkpoints. Agents cannot escalate their own permissions.                                                                                                                                    |
| **Artifact parity**         | Agent-generated entries are structurally indistinguishable from hand-crafted ones.                                                                                                                                                                                   |
| **Provider agnostic**       | LLM calls go through a thin provider interface. No vendor lock-in.                                                                                                                                                                                                   |
| **Prompt transparency**     | All prompts are versioned, templated, and stored separately from agent logic.                                                                                                                                                                                        |
| **Explicit conventions**    | Coupling to no-magic repo conventions is captured in a machine-readable schema.                                                                                                                                                                                      |

---

## 4. Multi-Agent Architecture

### 4.1 Agent Tiers

The system defines three tiers of agents based on behavioral complexity:

```mermaid
graph TB
    subgraph "Tier 1 — Full Agents (goal-directed, self-correcting)"
        Orchestrator[Orchestrator Agent<br/>Coordinates all specialists<br/>Dynamic planning & routing]
        Discovery[Discovery Agent<br/>Multi-step catalog reasoning<br/>Gap analysis → suggest → evaluate]
        Implementation[Implementation Agent<br/>Code generation with self-review<br/>Retry on lint/correctness failure]
        Review[Review Agent<br/>Conversational critique<br/>Structured feedback exchange]
    end

    subgraph "Tier 2 — Tool-Agents (single-task, orchestrator-dispatched)"
        Instrumentation[Instrumentation Agent<br/>Trace hook injection]
        Visualization[Visualization Agent<br/>Manim scene generation]
        Assessment[Assessment Agent<br/>Anki card generation]
    end

    subgraph "Tier 3 — Validators (called by agents to check work)"
        Lint[Lint Validator]
        Correctness[Correctness Validator]
        Consistency[Consistency Validator]
        Schema[Schema Compliance Validator]
    end

    Orchestrator --> Discovery
    Orchestrator --> Implementation
    Orchestrator --> Instrumentation
    Orchestrator --> Visualization
    Orchestrator --> Assessment
    Orchestrator --> Review

    Implementation --> Lint
    Implementation --> Correctness
    Review --> Consistency
    Review --> Schema
```

**Why three tiers?**

- **Full agents** exhibit goal-directed behavior: they plan, execute, observe results, and self-correct. The orchestrator decides _what to build next_. The implementation agent retries on lint failure with modified prompts. The review agent engages in multi-round critique.
- **Tool-agents** are transformations: input → LLM → output. Instrumentation, visualization, and assessment receive clear input (the implementation code) and produce clear output (instrumented code, Manim scene, Anki CSV). They don't need to plan or self-correct — the orchestrator handles retry logic for them.
- **Validators** are pure functions that check artifacts against criteria. Agents call validators to evaluate their own work before returning results to the orchestrator.

### 4.2 Agent Interface

```python
@runtime_checkable
class AgentInterface(Protocol):
    """Contract for all agents (full and tool-agents)."""

    name: str
    role: str                          # Human-readable role description
    system_prompt: str                 # Agent's persistent instructions
    allowed_tools: list[str]           # Tools this agent can access

    async def execute(
        self,
        task: AgentTask,
        context: AgentContext,
    ) -> AgentResult:
        """Execute the assigned task and return results."""
        ...
```

```python
@dataclass
class AgentTask:
    """Work unit dispatched by the orchestrator."""
    task_id: str
    task_type: str                     # "implement", "instrument", "visualize", etc.
    work_item: WorkItem
    input_artifacts: dict[str, str]    # Artifacts from prior agents
    constraints: dict[str, Any]        # Budget, max_retries, etc.

@dataclass
class AgentResult:
    """Response from an agent to the orchestrator."""
    agent_name: str
    task_id: str
    success: bool
    artifacts: dict[str, str]          # Output artifact paths
    tokens_used: int
    cost_usd: float
    diagnostics: list[dict[str, Any]]
    retry_requested: bool = False      # Agent requests another attempt
    retry_reason: str = ""             # Why the agent wants to retry
```

### 4.3 Orchestrator Agent

The orchestrator is the only Tier 1 agent that communicates with all others. It replaces the current `Pipeline` class with goal-directed coordination.

```python
class OrchestratorAgent:
    """Coordinates specialist agents to produce algorithm entries."""

    async def orchestrate(
        self,
        work_item: WorkItem,
        budget: BudgetAllocation,
    ) -> OrchestrationResult:
        """
        1. Plan: Determine which agents to invoke and in what order
        2. Implement: Dispatch to Implementation Agent, wait for result
        3. Validate: Implementation Agent self-validates via Lint + Correctness
        4. Fan-out: Dispatch to Instrumentation, Visualization, Assessment in parallel
        5. Integrate: Collect all artifacts, dispatch to Review Agent
        6. Finalize: Review Agent validates cross-artifact consistency
        7. Return: Bundled result with all artifacts and diagnostics
        """
```

**Dynamic planning**: Unlike the static pipeline, the orchestrator can make runtime decisions:

- Skip instrumentation for trivial algorithms (tier 1)
- Request extra validation rounds for complex algorithms (tier 4)
- Route to fallback model if primary model's budget is exhausted
- Re-dispatch to a different agent if the first attempt fails

### 4.4 High-Level Architecture

```mermaid
graph TB
    subgraph Control Plane
        CLI[CLI Interface]
        Scheduler[Cycle Scheduler]
        Budget[Budget Manager]
        Queue[Work Queue]
    end

    subgraph Agent System
        Orch[Orchestrator Agent]
        DiscAgent[Discovery Agent]
        ImplAgent[Implementation Agent]
        InstrAgent[Instrumentation<br/>Tool-Agent]
        VizAgent[Visualization<br/>Tool-Agent]
        AssessAgent[Assessment<br/>Tool-Agent]
        RevAgent[Review Agent]
    end

    subgraph Validators
        LintVal[Lint Validator]
        CorrectVal[Correctness Validator]
        ConsistVal[Consistency Validator]
        SchemaVal[Schema Compliance Validator]
    end

    subgraph Support Systems
        Prompts[Prompt Registry<br/>Versioned Templates]
        Observability[Observability<br/>Metrics & Tracing]
        ConventionSchema[Convention Schema<br/>no-magic-schema.yaml]
    end

    subgraph External
        LLM[LLM Provider<br/>Claude / OpenAI]
        GitHub[GitHub API]
        NoMagic[no-magic Repo]
    end

    CLI --> Queue
    Scheduler --> Queue
    Budget --> Orch

    Queue --> Orch
    Orch --> DiscAgent
    Orch --> ImplAgent
    Orch --> InstrAgent
    Orch --> VizAgent
    Orch --> AssessAgent
    Orch --> RevAgent

    ImplAgent --> LintVal
    ImplAgent --> CorrectVal
    RevAgent --> ConsistVal
    RevAgent --> SchemaVal

    ImplAgent --> LLM
    InstrAgent --> LLM
    VizAgent --> LLM
    AssessAgent --> LLM
    DiscAgent --> LLM
    RevAgent --> LLM

    ImplAgent --> Prompts
    InstrAgent --> Prompts
    VizAgent --> Prompts
    AssessAgent --> Prompts
    DiscAgent --> Prompts

    RevAgent --> ConventionSchema
    SchemaVal --> ConventionSchema

    Orch --> Observability
```

### 4.5 Agent Execution Flow

```mermaid
sequenceDiagram
    participant Orch as Orchestrator
    participant Impl as Implementation Agent
    participant Lint as Lint Validator
    participant Correct as Correctness Validator
    participant Instr as Instrumentation
    participant Viz as Visualization
    participant Assess as Assessment
    participant Rev as Review Agent

    Orch->>Impl: AgentTask(implement, work_item)

    loop Self-validation (max 3 attempts)
        Impl->>Impl: Generate code via LLM
        Impl->>Lint: Validate(code)
        Lint-->>Impl: Result
        Impl->>Correct: Validate(code)
        Correct-->>Impl: Result
        alt All pass
            Impl-->>Orch: AgentResult(success, artifacts)
        else Failure + retries remaining
            Impl->>Impl: Re-prompt with failure diagnostics
        else Failure + no retries
            Impl-->>Orch: AgentResult(failure, diagnostics)
        end
    end

    par Parallel tool-agent dispatch
        Orch->>Instr: AgentTask(instrument, implementation)
        Orch->>Viz: AgentTask(visualize, implementation)
        Orch->>Assess: AgentTask(assess, implementation)
    end

    Instr-->>Orch: AgentResult(instrumented code)
    Viz-->>Orch: AgentResult(manim scene)
    Assess-->>Orch: AgentResult(anki cards)

    Orch->>Rev: AgentTask(review, all artifacts)
    Rev->>Rev: Cross-artifact consistency check
    Rev-->>Orch: AgentResult(review verdict, feedback)
```

### 4.6 Component Breakdown

```mermaid
graph LR
    subgraph apprentice
        direction TB
        A[core/] --> A1[orchestrator.py<br/>Orchestrator Agent]
        A --> A2[budget.py<br/>Token & cost tracking]
        A --> A3[queue.py<br/>Work item management]
        A --> A4[observability.py<br/>Logging, metrics, alerts]
        A --> A5[pipeline.py<br/>Legacy pipeline adapter]

        B[agents/] --> B1[base.py<br/>AgentInterface protocol]
        B --> B2[discovery.py<br/>Discovery Agent]
        B --> B3[implementation.py<br/>Implementation Agent]
        B --> B4[instrumentation.py<br/>Instrumentation Tool-Agent]
        B --> B5[visualization.py<br/>Visualization Tool-Agent]
        B --> B6[assessment.py<br/>Assessment Tool-Agent]
        B --> B7[review.py<br/>Review Agent]

        C[validators/] --> C1[base.py<br/>Validator interface]
        C --> C2[lint.py]
        C --> C3[correctness.py]
        C --> C4[consistency.py]
        C --> C5[schema_compliance.py]

        D[providers/] --> D1[base.py<br/>Provider interface]
        D --> D2[anthropic.py]
        D --> D3[openai.py]

        E[prompts/] --> E1[Per-agent prompt templates]

        F[config/] --> F1[apprentice.toml]
        F --> F2[catalog.toml]
        F --> F3[no-magic-schema.yaml]
        F --> F4[templates/]
    end
```

---

## 5. Agent Specifications

### 5.1 Discovery Agent (Tier 1 — Full Agent)

**Goal**: Identify the best candidate algorithms to add to the catalog.

**Reasoning loop**:

1. Load current catalog and tier distribution
2. Analyze gaps (which tiers are underrepresented? what prerequisites are missing?)
3. Generate candidate list via LLM
4. Deduplicate against catalog (Levenshtein similarity ≥ 0.85)
5. Rank by pedagogical value and prerequisite coverage
6. If insufficient candidates after dedup, re-prompt with adjusted criteria

**Tools**: Catalog reader, LLM provider, name validator
**Self-correction**: Re-prompts if too many duplicates are filtered out

### 5.2 Implementation Agent (Tier 1 — Full Agent)

**Goal**: Generate a correct, stdlib-only Python implementation.

**Reasoning loop**:

1. Load reference implementations from the same tier
2. Generate implementation via LLM
3. Self-validate:
   - Run Lint Validator → if fail, re-prompt with lint diagnostics (max 2 retries)
   - Run Correctness Validator → if fail, re-prompt with error output (max 1 retry)
   - Check stdlib-only imports via AST
4. Return successful implementation or report failure with diagnostics

**Tools**: LLM provider, file writer, Lint Validator, Correctness Validator, AST analyzer
**Self-correction**: Modifies prompt based on validator feedback before retrying

### 5.3 Instrumentation Tool-Agent (Tier 2)

**Goal**: Add JSON trace hooks to an implementation.

**Execution**: Single LLM call. Receives implementation code, returns instrumented code with `{"step": int, "operation": str, "state": dict}` trace entries. No self-correction — orchestrator handles retries.

### 5.4 Visualization Tool-Agent (Tier 2)

**Goal**: Generate Manim animation steps for the scaffold template.

**Execution**: Single LLM call. Receives implementation code + Manim scaffold, returns animation steps. Orchestrator renders the scaffold. No self-correction.

### 5.5 Assessment Tool-Agent (Tier 2)

**Goal**: Generate Anki flashcard CSV.

**Execution**: Single LLM call. Receives implementation code, returns CSV with 4 card types (concept, complexity, implementation, comparison). Basic CSV validation. No self-correction.

### 5.6 Review Agent (Tier 1 — Full Agent)

**Goal**: Validate cross-artifact consistency and provide actionable feedback.

**Reasoning loop**:

1. Receive all generated artifacts (implementation, instrumented, manim, anki)
2. Run Consistency Validator — check name, signature, complexity agreement
3. Run Schema Compliance Validator — check convention conformance
4. If structural checks fail: return FAIL with specific diagnostics
5. For semantic issues: generate LLM-based assessment of pedagogical tone consistency
6. Compile review report with per-artifact feedback

**Tools**: LLM provider, Consistency Validator, Schema Compliance Validator
**Self-correction**: Does not retry its own work — provides feedback for other agents to act on

---

## 6. Validators

Validators are pure functions that agents call to check their work. They replace the current gate system but with a key difference: agents invoke validators directly rather than the pipeline calling gates between stages.

| Validator         | Called By            | Checks                                            | Blocking                        |
| ----------------- | -------------------- | ------------------------------------------------- | ------------------------------- |
| Lint              | Implementation Agent | Syntax, docstrings, type annotations, file size   | Yes                             |
| Correctness       | Implementation Agent | Subprocess execution, test assertions             | Yes                             |
| Consistency       | Review Agent         | Cross-artifact name/signature/complexity match    | Yes (structural), No (semantic) |
| Schema Compliance | Review Agent         | Convention conformance per `no-magic-schema.yaml` | Yes                             |

### Validator Interface

```python
class ValidatorInterface(Protocol):
    """Contract for all validators."""
    name: str

    def validate(
        self,
        artifacts: dict[str, str],
        work_item: WorkItem,
    ) -> ValidationResult:
        """Check artifacts against criteria."""
        ...

@dataclass
class ValidationResult:
    validator_name: str
    passed: bool
    issues: list[ValidationIssue]

@dataclass
class ValidationIssue:
    severity: Literal["error", "warning", "info"]
    message: str
    artifact: str      # Which artifact has the issue
    suggestion: str    # Actionable fix hint
```

The key improvement over the gate system: validators return structured `ValidationIssue` objects with `suggestion` fields. This gives the calling agent enough information to modify its prompt and retry intelligently — not just "FAIL" but "FAIL because the docstring is missing a Returns section."

---

## 7. Containment System

### 7.1 Budget Manager

```mermaid
graph TB
    subgraph Budget Hierarchy
        Global[Global Budget<br/>Monthly token ceiling<br/>Monthly cost ceiling USD]
        Cycle[Cycle Budget<br/>Per-run token limit<br/>Per-run cost limit<br/>Max algorithms per cycle]
        WorkItem[Work Item Budget<br/>Allocated at discovery<br/>Tracked per agent]
        Agent[Agent Budget<br/>Per-agent token cap<br/>Hard ceiling on provider call]
    end

    Global --> Cycle --> WorkItem --> Agent
```

**Four-level hierarchy**: Global → Cycle → Work Item → Agent. Each agent receives a token allocation from the orchestrator. The provider interface enforces a hard `max_tokens` ceiling on every LLM call.

**Agent-level budgeting**: Full agents (with retry loops) consume more budget than tool-agents. The orchestrator allocates budget proportionally:

- Implementation Agent: 40% of work item budget (may retry 2-3 times)
- Tool-agents (3 total): 15% each
- Review Agent: 15%

### 7.2 Rate Limiting

| Limit                              | Default | Configurable  |
| ---------------------------------- | ------- | ------------- |
| Max PRs per day                    | 2       | Yes           |
| Max PRs per week                   | 5       | Yes           |
| Max algorithms per cycle           | 3       | Yes           |
| Max concurrent work items          | 1       | Yes           |
| Cooldown between cycles            | 4 hours | Yes           |
| Max agent retries (implementation) | 3       | No (hard cap) |
| Max revision rounds on PR review   | 2       | No (hard cap) |
| Max files per PR                   | 10      | Yes           |
| Max lines changed per PR           | 2000    | Yes           |

### 7.3 Circuit Breaker

```mermaid
stateDiagram-v2
    [*] --> Closed: System healthy
    Closed --> Open: 3 consecutive shelved work items<br/>OR cycle budget exceeded<br/>OR rate limit hit
    Open --> HalfOpen: Cooldown elapsed
    HalfOpen --> Closed: Next work item completes successfully
    HalfOpen --> Open: Next work item fails
    Open --> [*]: Manual reset required<br/>after 3 open→halfopen→open cycles
```

### 7.4 Input Sanitization

| Input Source                  | Sanitization                                                                         |
| ----------------------------- | ------------------------------------------------------------------------------------ |
| Algorithm names               | Whitelist: `[a-z0-9_]` only. Max 64 chars.                                           |
| GitHub issue descriptions     | Strip prompt control sequences before LLM context.                                   |
| Existing implementation files | Loaded as plain text, never executed. Flagged as untrusted in agent context.         |
| PR review comments            | Parsed for actionable feedback only.                                                 |
| Inter-agent messages          | Structured `AgentTask`/`AgentResult` dataclasses only — no free-form text injection. |

---

## 8. User Workflow — Assisted Mode (v1)

```mermaid
sequenceDiagram
    actor Dev as Developer
    participant CLI as apprentice CLI
    participant Orch as Orchestrator
    participant Impl as Implementation Agent
    participant Agents as Tool-Agents
    participant Rev as Review Agent
    participant GH as GitHub

    Dev->>CLI: apprentice suggest --tier 2 --limit 5
    CLI->>Orch: Dispatch to Discovery Agent
    Orch-->>CLI: Ranked candidate list
    CLI-->>Dev: Display candidates with rationale

    Dev->>CLI: apprentice build "quickselect"
    CLI->>Orch: Orchestrate full build
    Orch->>Impl: Generate implementation
    Impl->>Impl: Self-validate (lint + correctness)
    Impl-->>Orch: Implementation artifact

    par Parallel dispatch
        Orch->>Agents: Instrument + Visualize + Assess
    end
    Agents-->>Orch: All artifacts

    Orch->>Rev: Review all artifacts
    Rev-->>Orch: Review verdict

    Orch-->>CLI: Complete artifact bundle
    CLI-->>Dev: JSON result with all paths

    Dev->>CLI: apprentice preview
    CLI-->>Dev: Artifact contents preview

    Dev->>CLI: apprentice submit
    Orch->>GH: Create branch, push, open PR
    GH-->>Dev: PR notification
```

### CLI Commands

```
apprentice suggest [--tier N] [--limit N]     # Discovery Agent
apprentice build <algorithm>                   # Full orchestrated build
apprentice build --from-issue <issue-number>   # Build from GitHub issue
apprentice preview                             # Inspect last build artifacts
apprentice submit                              # Package and open PR
apprentice status                              # Budget usage, queue state
apprentice metrics [--last-7d]                 # Cost breakdown, agent stats
apprentice retry <work-item-id>                # Retry shelved item
apprentice reset-circuit                       # Manual circuit breaker reset
apprentice config                              # View/edit apprentice.toml
```

---

## 9. System Workflow — Autonomous Mode (v2)

```mermaid
sequenceDiagram
    participant Sched as Scheduler
    participant Budget as Budget Manager
    participant Queue as Work Queue
    participant Orch as Orchestrator
    participant Agents as Agent System
    participant CB as Circuit Breaker
    participant GH as GitHub
    actor Dev as Maintainer

    Sched->>Budget: Request cycle allocation
    Budget-->>Sched: Approved (3 algorithms, 50K token cap)

    Sched->>Orch: Start cycle
    Orch->>Orch: Dispatch Discovery Agent → candidates
    Orch->>Queue: Populate with top-N candidates

    loop For each work item
        Queue-->>Orch: Next work item
        Orch->>Agents: Full agent orchestration
        alt All agents succeed
            Orch->>GH: Open PR (agent cannot merge)
        else Agent failure after retries
            Orch->>CB: Record shelved work item
            CB-->>Orch: Circuit state
        end
        Orch->>Budget: Report usage
    end

    Sched->>Budget: Cycle complete

    Dev->>GH: Review PRs → Approve/Request changes
    GH-->>Orch: Review event (polling)
    Orch->>Agents: Review Agent processes feedback
```

---

## 10. Data Model

### 10.1 Entity Relationships

```mermaid
erDiagram
    WORK_ITEM {
        string id PK
        string algorithm_name
        int tier
        string status "queued | in_progress | completed | revision_requested | shelved | archived"
        string source "discovery | manual | github_issue"
        string rationale
        int allocated_tokens
        int actual_tokens
        string last_failed_agent
        datetime created_at
        datetime completed_at
    }

    ARTIFACT_BUNDLE {
        string id PK
        string work_item_id FK
        int revision_number
        string parent_bundle_id FK "nullable"
        string implementation_path
        string instrumented_path
        string manim_scene_path
        string anki_deck_path
        string readme_section
        string template_version
        string pr_url
        datetime created_at
    }

    AGENT_RESULT {
        string id PK
        string work_item_id FK
        string agent_name
        string task_type
        bool success
        int tokens_used
        float cost_usd
        string diagnostics "structured JSON"
        int attempt_number
        datetime executed_at
    }

    VALIDATION_RESULT {
        string id PK
        string agent_result_id FK
        string validator_name
        bool passed
        string issues "structured JSON"
        datetime evaluated_at
    }

    BUDGET_LOG {
        string id PK
        string cycle_id
        string work_item_id FK
        string agent_name
        string provider
        string model
        int estimated_tokens
        int actual_tokens
        float estimated_cost_usd
        float actual_cost_usd
        datetime logged_at
    }

    CYCLE {
        string id PK
        datetime started_at
        datetime ended_at
        int items_attempted
        int items_completed
        int items_shelved
        int total_tokens
        float total_cost_usd
        string circuit_state
    }

    WORK_ITEM ||--o{ ARTIFACT_BUNDLE : "produces (versioned)"
    WORK_ITEM ||--o{ AGENT_RESULT : "processed by"
    AGENT_RESULT ||--o{ VALIDATION_RESULT : "validated by"
    WORK_ITEM ||--o{ BUDGET_LOG : tracked_in
    CYCLE ||--o{ WORK_ITEM : contains
    CYCLE ||--o{ BUDGET_LOG : aggregates
```

### 10.2 Work Item State Machine

```mermaid
stateDiagram-v2
    [*] --> queued: Discovery or manual creation

    queued --> in_progress: Orchestrator picks item
    queued --> archived: Human cancels

    in_progress --> completed: All agents succeed, PR opened
    in_progress --> shelved: Agent fails after retries exhausted
    in_progress --> archived: Circuit breaker kills cycle

    completed --> revision_requested: PR review requests changes

    revision_requested --> in_progress: Review Agent processes feedback

    shelved --> queued: Human manually re-queues

    archived --> [*]: Terminal state
```

---

## 11. Configuration — `apprentice.toml`

```toml
[budget.global]
monthly_token_ceiling = 2_000_000
monthly_cost_ceiling_usd = 50.0

[budget.cycle]
max_tokens_per_cycle = 100_000
max_cost_per_cycle_usd = 5.0
max_algorithms_per_cycle = 3

[budget.agent]
max_tokens_per_agent_call = 20_000
implementation_budget_pct = 40       # % of work item budget
tool_agent_budget_pct = 15           # each of 3 tool-agents
review_budget_pct = 15

[rate_limits]
max_prs_per_day = 2
max_prs_per_week = 5
max_concurrent_items = 1
cooldown_hours = 4
max_files_per_pr = 10
max_lines_per_pr = 2000

[agents]
max_implementation_retries = 3       # Self-validation retry cap
max_review_rounds = 2
max_tool_agent_retries = 1           # Orchestrator retries for tool-agents

[circuit_breaker]
failure_threshold = 3
half_open_probe_after_minutes = 60
max_open_cycles_before_manual_reset = 3

[provider]
default = "anthropic"
model = "claude-sonnet-4-20250514"
fallback_model = "claude-haiku-4-5-20251001"
fallback_trigger = "budget_warning"

[observability]
log_level = "INFO"
log_format = "json"
log_path = "${HOME}/.apprentice/logs"
metrics_enabled = true
alert_on_circuit_open = true
alert_webhook = ""

[templates]
version = "1.0.0"
base_path = "config/templates"
```

---

## 12. Version Roadmap

| Version  | Scope                                                                                           | Mode                     |
| -------- | ----------------------------------------------------------------------------------------------- | ------------------------ |
| **v0.1** | CLI scaffold, provider interface, single-stage implementation generation                        | Assisted only            |
| **v0.2** | Full pipeline (all stages with parallel artifact generation), quality gates                     | Assisted only            |
| **v0.3** | Multi-agent refactor: agent interfaces, orchestrator, implementation agent with self-validation | Assisted only            |
| **v0.4** | All specialist agents, review agent, validator integration                                      | Assisted only            |
| **v1.0** | Stable assisted mode with multi-agent orchestration, ≥95% success rate                          | **Assisted — release**   |
| **v1.1** | Scheduler, work queue, cycle management                                                         | Autonomous foundations   |
| **v1.2** | Circuit breaker, rate limiting, full containment                                                | Autonomous safeguards    |
| **v1.3** | Discovery Agent (autonomous candidate selection)                                                | Autonomous discovery     |
| **v1.4** | Observability: agent metrics, cost dashboard, alerting                                          | Autonomous monitoring    |
| **v2.0** | Full autonomous mode. Read-only launch (opens PRs, human merges).                               | **Autonomous — release** |
| **v2.1** | Review Agent feedback loop (revises from PR review comments)                                    | Autonomous refinement    |

---

## 13. Repository Structure

```
no-magic-ai/apprentice/
├── src/
│   └── apprentice/
│       ├── __init__.py
│       ├── cli.py                    # CLI entry point
│       ├── core/
│       │   ├── orchestrator.py       # Orchestrator Agent
│       │   ├── pipeline.py           # Legacy pipeline (adapter for agents)
│       │   ├── budget.py             # Token & cost tracking
│       │   ├── queue.py              # Work item management
│       │   ├── circuit_breaker.py    # Failure containment
│       │   ├── scheduler.py          # Autonomous cycle scheduling
│       │   └── observability.py      # Structured logging, metrics
│       ├── agents/
│       │   ├── base.py               # AgentInterface protocol
│       │   ├── discovery.py          # Discovery Agent (Tier 1)
│       │   ├── implementation.py     # Implementation Agent (Tier 1)
│       │   ├── instrumentation.py    # Instrumentation Tool-Agent (Tier 2)
│       │   ├── visualization.py      # Visualization Tool-Agent (Tier 2)
│       │   ├── assessment.py         # Assessment Tool-Agent (Tier 2)
│       │   └── review.py             # Review Agent (Tier 1)
│       ├── validators/
│       │   ├── base.py               # ValidatorInterface protocol
│       │   ├── lint.py
│       │   ├── correctness.py
│       │   ├── consistency.py
│       │   └── schema_compliance.py
│       ├── stages/                   # Legacy stages (kept for backward compat)
│       │   └── ...
│       ├── gates/                    # Legacy gates (kept for backward compat)
│       │   └── ...
│       ├── providers/
│       │   ├── base.py
│       │   ├── anthropic.py
│       │   └── openai.py
│       ├── prompts/
│       │   ├── orchestrator.yaml
│       │   ├── discovery.yaml
│       │   ├── implementation.yaml
│       │   ├── instrumentation.yaml
│       │   ├── visualization.yaml
│       │   ├── assessment.yaml
│       │   └── review.yaml
│       └── models/
│           ├── work_item.py
│           ├── artifact.py
│           ├── budget.py
│           ├── agent.py              # AgentTask, AgentResult
│           └── cycle.py
├── config/
│   ├── apprentice.toml
│   ├── catalog.toml
│   ├── no-magic-schema.yaml
│   └── templates/
│       └── manim_scene.py.j2
├── tests/
├── pyproject.toml
├── README.md
└── LICENSE
```

---

## 14. Open Design Questions

| #   | Question            | Options                            | Decision                                                        |
| --- | ------------------- | ---------------------------------- | --------------------------------------------------------------- |
| 1   | State persistence   | SQLite vs. flat JSON files         | **SQLite** — queryable budget history, transaction safety.      |
| 2   | Template engine     | Jinja2 vs. string templates        | **Jinja2** — one dependency, massive complexity reduction.      |
| 3   | Manim validation    | Headless render vs. AST-only check | **Headless render** — AST can't catch runtime animation errors. |
| 4   | Anki export format  | `.apkg` vs. CSV                    | **CSV for v1.0**, `.apkg` as enhancement.                       |
| 5   | Autonomous trigger  | Cron vs. GitHub Actions            | **GitHub Actions** — runs where the repo lives.                 |
| 6   | PR review ingestion | Webhook vs. poll                   | **Poll** — simpler, fits the cycle model.                       |

### New Design Questions (Multi-Agent)

| #   | Question                     | Context                                                                                                                                                  |
| --- | ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 7   | Agent SDK                    | Build custom lightweight orchestrator on `anthropic` SDK tool-use, or adopt `claude-agent-sdk`? Custom gives full control; SDK gives session management. |
| 8   | Agent-to-agent communication | Structured `AgentTask`/`AgentResult` only, or allow free-form messages between agents? Structured is safer and more debuggable.                          |
| 9   | Tool-agent promotion         | When should a tool-agent be promoted to full agent? Criteria: needs >1 LLM call, benefits from self-correction, has conditional logic.                   |
| 10  | Legacy stage compatibility   | Keep `stages/` and `gates/` as importable modules for backward compatibility, or remove entirely? Adapter pattern preserves tests.                       |
| 11  | Async vs sync                | Agents are I/O-bound (LLM calls). Use `asyncio` throughout, or keep synchronous with `ThreadPoolExecutor`? Async is cleaner for agent loops.             |
