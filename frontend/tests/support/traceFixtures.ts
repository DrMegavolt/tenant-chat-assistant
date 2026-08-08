export const RECORD_WIRE = {
  turn_id: "turn-1",
  session_id: "session-1",
  trace_id: "trace-gateb-8",
  recorded_at: "2026-08-03T20:00:00+00:00",
  outcome: "answered",
  component_manifest_hash: "8".repeat(64),
  diagnosis_causes: ["grounding_or_citation_error"],
  diagnosis_statuses: ["detected"],
  turn_index: 8,
  trace_schema_version: "1"
};

export const SUSPECTED_RECORD_WIRE = {
  ...RECORD_WIRE,
  turn_id: "turn-2",
  trace_id: "trace-gateb-7",
  component_manifest_hash: "7".repeat(64),
  diagnosis_causes: ["model_behavior"],
  diagnosis_statuses: ["suspected"],
  turn_index: 7
};

export const TRACE_READ_WIRE_CONTENT = {
  schema_version: "1",
  turn_index: 8,
  manifest_hash: "8".repeat(64),
  routing: {
    rule: "answer",
    intent: "general",
    score: 4,
    threshold: 2.5,
    policy_version: "intent-routing@1",
    candidates: [
      { intent: "general", score: 4, matched_signals: ["discount"] },
      { intent: "booking", score: 0, matched_signals: [] }
    ]
  },
  retrieval: {
    query: "Is there a discount for quarterly window cleaning?",
    sufficient: true,
    retriever_version: "v1",
    reranker: "bigram-overlap",
    min_evidence_score: 0.5,
    embedding_model: "scripted-embedder.v1",
    generation_id: "gen-1",
    filters: { tenant_id: "clearview" },
    budget: { max_sources: 3, max_context_tokens: 1500 },
    parameters: {},
    candidates: [
      { source_id: "clearview-windows-5", score: 0.8, generation_id: "gen-1" },
      { source_id: "clearview-windows-6", score: 0.4, generation_id: "gen-1" }
    ],
    evidence: [{ source_id: "clearview-windows-5", score: 0.8, generation_id: "gen-1" }]
  },
  prompt: {
    template_ref: "dispatch-system@4",
    content_hash: "deadbeef",
    bindings: { business_name: "Clearview Property Care" },
    excluded: [],
    messages: [
      {
        role: "system",
        segments: [["briefing", "trusted", "You are the Clearview assistant."]],
        tool_calls: [],
        tool_call_id: null
      },
      {
        role: "user",
        segments: [
          ["visitor", "untrusted", "Is there a discount?"],
          ["evidence-1", "untrusted", "Commercial contracts receive quarterly cleaning schedules."]
        ],
        tool_calls: [],
        tool_call_id: null
      }
    ]
  },
  model: { name: "scripted", usage: {} },
  output: {
    answer: "Yes, quarterly plans save 20%.",
    raw: "Yes, quarterly plans save 20%. [evidence:clearview-windows-99]",
    claims: ["clearview-windows-99", "clearview-windows-5"]
  },
  verdicts: {
    citations: [{ source_id: "clearview-windows-5", title: "Commercial contracts" }],
    citation_invalid: ["clearview-windows-99"],
    refused_tools: [],
    claims_invalid: []
  },
  tools: {
    tool_calls: [
      { call_id: "call-1", name: "book_appointment", arguments: {} },
      { call_id: "call-2", name: "create_lead", arguments: {} }
    ],
    tool_results: [
      {
        call_id: "call-1",
        result: '{"error": "booking_already_proposed", "reference": "BK-1"}'
      },
      { call_id: "call-2", result: '{"lead_id": "lead-1"}' }
    ],
    committed: []
  },
  outcome: { status: "answered", rounds: 1, failure: null },
  component_manifest: { graph: "dispatch@2" },
  diagnoses: [
    {
      cause: "grounding_or_citation_error",
      stage: "validation",
      role: "primary",
      status: "detected",
      confidence: "high",
      evidence: ["citation_invalid:clearview-windows-99"],
      detector_version: "diagnosis@1"
    }
  ]
};

export const SUSPECTED_READ_WIRE_CONTENT = {
  ...TRACE_READ_WIRE_CONTENT,
  turn_index: 7,
  manifest_hash: "7".repeat(64),
  outcome: { status: "answered", rounds: 1, failure: "unresolved" },
  diagnoses: [
    {
      cause: "model_behavior",
      stage: "model",
      role: "primary",
      status: "suspected",
      confidence: "medium",
      evidence: ["outcome.failure:unresolved"],
      detector_version: "diagnosis@1"
    }
  ]
};

export const PARTIAL_READ_WIRE_CONTENT = {
  schema_version: "1",
  turn_index: 3,
  manifest_hash: "3".repeat(64),
  retrieval: {
    query: "What are your hours?",
    sufficient: false,
    retriever_version: "unavailable",
    reranker: null,
    min_evidence_score: null,
    embedding_model: "",
    generation_id: null,
    filters: {},
    budget: {},
    parameters: {},
    candidates: [],
    evidence: []
  },
  model: { name: "", usage: {} },
  output: { answer: "", raw: "", claims: [] },
  verdicts: { citations: [], citation_invalid: [], refused_tools: [], claims_invalid: [] },
  tools: { tool_calls: [], tool_results: [], committed: [] },
  outcome: { status: "abstained", rounds: 0, failure: "insufficient_evidence" },
  diagnoses: [
    {
      cause: "ingestion_or_index_error",
      stage: "retrieval",
      role: "primary",
      status: "detected",
      confidence: "high",
      evidence: ["retrieval.retriever_version:unavailable"],
      detector_version: "diagnosis@1"
    }
  ]
};

export function wireTraceContent(turnId: string, content: Record<string, unknown>) {
  return {
    turn_id: turnId,
    tenant_id: "clearview",
    session_id: "session-1",
    trace_id:
      turnId === "turn-2" ? "trace-gateb-7" : `trace-gateb-${turnId === "turn-3" ? "3" : "8"}`,
    recorded_at: "2026-08-03T20:00:00+00:00",
    content,
    projections: []
  };
}

export const GOLD_WIRE = {
  cases: [
    {
      case_id: "clearview-window-fabricated",
      tenant_id: "clearview",
      scenario: "fabricated_citation",
      query: "Is there a discount for quarterly window cleaning?",
      gold_chunks: [
        {
          source_id: "clearview-windows-5",
          text: "Commercial contracts receive quarterly cleaning schedules and a dedicated account contact."
        }
      ]
    }
  ]
};

export const REPLAY_WIRE = {
  turn_id: "turn-1",
  recorded_at: "2026-08-03T20:00:00+00:00",
  manifest_hash: "8".repeat(64),
  current_manifest_hash: "c".repeat(64),
  manifest_changed: true,
  stochastic: true,
  components: [
    { name: "graph", stored: "dispatch@2", current: "dispatch@2", changed: false },
    {
      name: "prompt_template",
      stored: '"dispatch-system@3"',
      current: '"dispatch-system@4"',
      changed: true
    },
    { name: "model", stored: '"scripted"', current: '"gpt-9"', changed: true }
  ],
  original: {
    content_hash: "hash-original",
    model_name: "scripted",
    output_raw: "Yes, quarterly plans save 20%. [evidence:clearview-windows-99]"
  },
  replayed: {
    content_hash: "hash-replayed",
    model_name: "gpt-9",
    output_raw: "Replayed trial output."
  }
};

/** A captured `OBS-006` executed-graph section for one first run. */
export const EXECUTED_GRAPH_SECTION = {
  run_kind: "send",
  started_at: "2026-08-03T20:00:00.001+00:00",
  ended_at: "2026-08-03T20:00:00.050+00:00",
  duration_ms: 49,
  nodes: [
    {
      name: "route",
      attempt: 1,
      edge: "branch:to:route",
      status: "ok",
      interrupted: false,
      replayed: false,
      started_at: "2026-08-03T20:00:00.001+00:00",
      ended_at: "2026-08-03T20:00:00.004+00:00",
      duration_ms: 3
    },
    {
      name: "model",
      attempt: 1,
      edge: "branch:to:model",
      status: "ok",
      interrupted: false,
      replayed: false,
      started_at: "2026-08-03T20:00:00.005+00:00",
      ended_at: "2026-08-03T20:00:00.045+00:00",
      duration_ms: 40
    },
    {
      name: "finalize",
      attempt: 1,
      edge: "branch:to:finalize",
      status: "ok",
      interrupted: false,
      replayed: false,
      started_at: "2026-08-03T20:00:00.046+00:00",
      ended_at: "2026-08-03T20:00:00.050+00:00",
      duration_ms: 4
    }
  ],
  edges: [
    { source: "__start__", target: "route", label: "branch:to:route" },
    { source: "route", target: "model", label: "branch:to:model" },
    { source: "model", target: "finalize", label: "branch:to:finalize" }
  ]
};

export const CAPTURED_READ_WIRE_CONTENT = {
  ...TRACE_READ_WIRE_CONTENT,
  schema_version: "2",
  executed_graph: EXECUTED_GRAPH_SECTION
};

/** A captured resumed run: the interrupted node is replayed first. */
export const RESUMED_READ_WIRE_CONTENT = {
  ...TRACE_READ_WIRE_CONTENT,
  schema_version: "2",
  outcome: { status: "answered", rounds: 2, failure: null },
  executed_graph: {
    run_kind: "resume",
    started_at: "2026-08-03T20:01:00.001+00:00",
    ended_at: "2026-08-03T20:01:00.030+00:00",
    duration_ms: 29,
    nodes: [
      {
        name: "confirm_booking",
        attempt: 1,
        edge: "branch:to:confirm_booking",
        status: "ok",
        interrupted: false,
        replayed: true,
        started_at: "2026-08-03T20:01:00.001+00:00",
        ended_at: "2026-08-03T20:01:00.006+00:00",
        duration_ms: 5
      },
      {
        name: "commit_booking",
        attempt: 1,
        edge: "branch:to:commit_booking",
        status: "ok",
        interrupted: false,
        replayed: false,
        started_at: "2026-08-03T20:01:00.007+00:00",
        ended_at: "2026-08-03T20:01:00.020+00:00",
        duration_ms: 13
      },
      {
        name: "model",
        attempt: 1,
        edge: "branch:to:model",
        status: "ok",
        interrupted: false,
        replayed: false,
        started_at: "2026-08-03T20:01:00.021+00:00",
        ended_at: "2026-08-03T20:01:00.026+00:00",
        duration_ms: 5
      },
      {
        name: "finalize",
        attempt: 1,
        edge: "branch:to:finalize",
        status: "ok",
        interrupted: false,
        replayed: false,
        started_at: "2026-08-03T20:01:00.027+00:00",
        ended_at: "2026-08-03T20:01:00.030+00:00",
        duration_ms: 3
      }
    ],
    edges: [
      { source: "__start__", target: "confirm_booking", label: "branch:to:confirm_booking" },
      { source: "confirm_booking", target: "commit_booking", label: "branch:to:commit_booking" },
      { source: "commit_booking", target: "model", label: "branch:to:model" },
      { source: "model", target: "finalize", label: "branch:to:finalize" }
    ]
  }
};

/** A captured run whose graph crashed at the model node: it ends there. */
export const CRASHED_READ_WIRE_CONTENT = {
  ...TRACE_READ_WIRE_CONTENT,
  schema_version: "2",
  outcome: { status: "failed", rounds: 0, failure: "application_error" },
  executed_graph: {
    run_kind: "send",
    started_at: "2026-08-03T20:02:00.001+00:00",
    ended_at: null,
    duration_ms: null,
    nodes: [
      {
        name: "route",
        attempt: 1,
        edge: "branch:to:route",
        status: "ok",
        interrupted: false,
        replayed: false,
        started_at: "2026-08-03T20:02:00.001+00:00",
        ended_at: "2026-08-03T20:02:00.004+00:00",
        duration_ms: 3
      },
      {
        name: "model",
        attempt: 1,
        edge: "branch:to:model",
        status: "error",
        interrupted: false,
        replayed: false,
        started_at: "2026-08-03T20:02:00.005+00:00",
        ended_at: null,
        duration_ms: null
      }
    ],
    edges: [
      { source: "__start__", target: "route", label: "branch:to:route" },
      { source: "route", target: "model", label: "branch:to:model" }
    ]
  }
};
/** A v3 record: the retrieval section carries the resolved query and plan beside the original. */
export const V3_READ_WIRE_CONTENT = {
  ...CAPTURED_READ_WIRE_CONTENT,
  schema_version: "3",
  turn_index: 4,
  retrieval: {
    ...CAPTURED_READ_WIRE_CONTENT.retrieval,
    original_message: "How much does it cost?",
    resolved_query: "Clearview HVAC maintenance How much does it cost?",
    plan: {
      planner_version: "query-planning@1",
      tenant_id: "clearview",
      workflow: "general",
      query: "Clearview HVAC maintenance How much does it cost?",
      mode: "resolve_pronoun",
      topic: "Clearview HVAC maintenance",
      entities: ["Clearview HVAC maintenance"],
      history_used: 1,
      reset: false
    }
  }
};
