# Steadfast Support Ticket Triage Pipeline

## Overview

You're building a **support ticket triage pipeline** for Steadfast, a B2B SaaS company. The pipeline processes incoming support tickets and must:

1. **Classify** each ticket into a category and priority level
2. **Generate** a short initial response to the customer

You're given a knowledge base of 300 historical tickets (CSV) and a 40-ticket labeled dev set for evaluation. A hidden test set (which you do not have access to) will be used in the final assessment.

Your job is to implement the pipeline described below, iterate on it using the dev set, and submit your best version.

---

## The Pipeline

You must implement a pipeline with the following stages. Each stage has a clear input and output. You have freedom in *how* you implement each stage, but all stages must be present and functional.

```
┌─────────────┐    ┌──────────────┐    ┌──────────────────────┐
│  1. LOAD     │───▶│ 2. PREPROCESS │───▶│ 3. LLM CLASSIFICATION │
│  DATA        │    │              │    │                      │
└─────────────┘    └──────────────┘    └──────────┬───────────┘
                                                  │
                                                  ▼
┌─────────────┐    ┌──────────────┐    ┌──────────────────────┐
│  6. EVALUATE │◀───│ 5. HEURISTICS │◀───│ 4. VALIDATE          │
│              │    │  & POST-PROC │    │    OUTPUT             │
└──────┬──────┘    └──────────────┘    └──────────────────────┘
       │
       ▼
┌─────────────┐    ┌──────────────┐
│  7. ERROR    │───▶│ 8. ITERATE   │──── (loop back to any stage)
│  ANALYSIS    │    │              │
└─────────────┘    └──────────────┘
```

### Stage 1 — Load Data

Read the knowledge base CSV and dev set JSON. Parse, inspect structure, understand what you're working with. Pay attention to the vocabulary and concepts that appear in the knowledge base — Steadfast has platform-specific features and terminology that will appear in incoming tickets.

### Stage 2 — Preprocess

Clean and normalize the knowledge base. Prepare it for use as context in later stages. Document any decisions you make here.

### Stage 3 — LLM Classification

Send each incoming ticket to an LLM with a structured prompt. The model should return JSON with category, priority, and a customer response. Use the knowledge base as context (RAG, in-context examples, embeddings — your choice). Design your system prompt carefully.

**Important:** The knowledge base contains Steadfast-specific features, internal terminology, and historical resolution context that the model won't know about otherwise. Your responses should be grounded in this context — referencing specific workarounds, configuration steps, known issues, or KB articles where relevant. Generic responses like "we're looking into it" that could apply to any product are significantly lower quality than responses that demonstrate knowledge of Steadfast's platform.

### Stage 4 — Validate Output

Check every LLM response for: valid JSON structure, allowed enum values for category and priority, required fields present, response not empty. Handle failures gracefully — fall back to `unknown` or flag for human review. Track validation failure rates.

### Stage 5 — Heuristics & Post-Processing

Add rule-based corrections on top of the LLM output. Examples: tickets mentioning specific keywords might need priority adjustments, certain patterns might reliably indicate a category, plan-based business rules might apply. These rules should be informed by patterns you observe in the data and in your error analysis.

### Stage 6 — Evaluate

Run the full pipeline on the dev set. Compute:

- Category accuracy (exact match + partial credit for acceptable alternatives)
- Priority accuracy
- A response quality score (you define the metric — justify your choice)
- Per-category and per-priority breakdowns

Your response quality metric should capture whether the response is actually helpful to the customer — not just whether it's polite and the right length. Consider whether the response references the correct issue, provides actionable next steps, and avoids giving advice that's related but wrong (e.g., fixing the wrong endpoint, referencing the wrong integration provider).

Output a structured report.

### Stage 7 — Error Analysis

Review incorrect predictions. Categorize them by root cause and document your findings.

### Stage 8 — Iterate

Based on error analysis, improve any stage: prompt, preprocessing rules, heuristics, context selection, validation logic. Rerun eval. Repeat.

---

## Output Format

The pipeline must produce a JSON result per ticket:

```json
{
  "ticket_id": "EVAL-001",
  "category": "bug",
  "priority": "high",
  "response": "Hi — thanks for reaching out. We're looking into the dashboard issue...",
  "confidence": 0.85,
  "flags": []
}
```

| Field | Required | Description |
|---|---|---|
| `ticket_id` | Yes | From the input ticket |
| `category` | Yes | One of: `billing`, `bug`, `feature_request`, `account`, `integration`, `onboarding`, `security`, `performance` |
| `priority` | Yes | One of: `low`, `medium`, `high`, `critical` |
| `response` | Yes | Initial customer-facing response |
| `confidence` | No | Model's confidence (0-1). Encouraged but optional. |
| `flags` | No | Array of strings for anything notable: `["ambiguous_category", "possible_duplicate", "escalate_to_human"]` |

---

## Constraints

- Use any LLM provider (OpenAI, Anthropic, open-source — your choice)
- No fine-tuning. Prompt engineering and RAG only.
- Total latency per ticket should be under 30 seconds
- Pipeline must be re-runnable end-to-end from a single command

---

## Rules

1. **You may use AI coding tools** (Copilot, Cursor, Claude, ChatGPT, etc.). We expect you to. How you use them is part of what we evaluate.
2. **Time budget: ~6 hours of focused work.** Not timed, but scope accordingly. We value a well-reasoned 80% solution over a sloppy 95%.
3. **Include a git log.** Init a repo and commit as you go. We want to see how your work evolved.
4. **Code must run.** We will clone, install deps, set an API key env var, and run your pipeline. If it breaks, that's a signal.

---

## Deliverables

1. **The pipeline code** — all 8 stages, modular, runnable
2. **Evaluation output** — your latest eval results as JSON
3. **Write-up (max 2 pages)** covering:
   - Data exploration: how you approached the knowledge base and eval set
   - Pipeline design decisions: why you chose your approach at each stage
   - Iteration log: what you tried, what worked, what didn't (with metrics)
   - Response quality metric: what you chose and why
   - What you'd do differently with more time

---

## Project Structure

```
/src
  pipeline.py          # main entry point
  preprocess.py        # stage 2
  agent.py             # stage 3 (LLM classification)
  validate.py          # stage 4
  postprocess.py       # stage 5
  evaluate.py          # stage 6
  analyze.py           # stage 7 (error analysis)
/data
  knowledge_base.csv   # 300 historical tickets
  eval_set.json        # 40-ticket labeled dev set
/output
  eval_results.json    # your latest eval run
  error_analysis.json  # your error analysis output
WRITEUP.md
README.md
requirements.txt
.env.example
```

You may restructure however you like, but all stages must be identifiable and the full pipeline must run from a single entry point.

---

## Setup

```bash
git clone <this-repo>
cd steadfast-triage-assignment
pip install -r requirements.txt
cp .env.example .env
# Add your API key to .env
```

## Run

```bash
# Run pipeline on eval set
python src/pipeline.py

# Run pipeline + evaluation + error analysis
python src/pipeline.py --eval
```

---

## FAQ

**Q: Can I use frameworks like LangChain, LlamaIndex, DSPy?**
A: Yes. But if your pipeline is opaque framework calls, we want evidence you understand what's happening at each stage.

**Q: What model should I use?**
A: Your choice. Model selection is a decision we want to see you justify.

**Q: How is the hidden test set scored?**
A: Same pipeline — category accuracy, priority accuracy, response quality (scored by an LLM judge with a rubric we share post-review). The test set has harder edge cases. We expect a 5-15% accuracy drop from dev to test.

**Q: What if my accuracy is low?**
A: Raw accuracy is not the primary signal. Sound evaluation + clear error analysis + well-reasoned iteration beats high accuracy with no explanation.

**Q: Do I need to implement all 8 stages?**
A: Yes. A stage can be minimal (e.g., "no heuristics applied — here's why") but it must be present. Skipping a stage entirely is a signal.

**Q: How important is the knowledge base for classification?**
A: Very. Some tickets reference Steadfast-specific features and terminology that an LLM won't know about without KB context. Your pipeline should use the KB, not just classify from ticket text alone.

**Q: How is response quality scored on the hidden test set?**
A: By an LLM judge evaluating relevance, tone, and actionability. Responses that reference specific Steadfast features, workarounds, or known issues from the KB score higher than generic empathetic responses. Getting the right advice for the wrong issue (e.g., fixing the wrong endpoint) is penalized.
