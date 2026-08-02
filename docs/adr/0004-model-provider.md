# 0004 — Local OpenAI-compatible model provider by default

- **Status:** Accepted
- **Date:** 2026-07-31
- **Affects:** `AI-001`, `AI-002`, `RAG-008`, `DEP-003`

## Context

The platform needs a language model for the agent loop and for grounded answer
generation, and an embedding model for retrieval. Two things pull in opposite
directions.

Development cost pulls toward local inference. `RAG-008` requires an evaluation
suite that runs on every change to a prompt, retriever, chunker, or model. Those
suites are run hundreds of times while being built, each run covering a full
dataset. Metering every one of those calls against a hosted API turns iteration
speed into a billing decision, which is the wrong incentive when the goal is
retrieval quality.

Demonstrability pulls toward a hosted provider. A hosted deployment cannot reach a
model server on a developer's LAN, so a locally-hosted model means there is no
public URL a reader can click.

## Decision

**Default to a local OpenAI-compatible server. Reach it through a
provider-neutral interface so a hosted provider is a configuration change.**

- `LLM_BASE_URL` defaults to `http://localhost:1234/v1`, which LM Studio, Ollama,
  and llama.cpp all serve.
- The chat, tool-call, streaming, usage, and error contracts are defined in
  `packages/core` as ports. No domain or workflow code names a provider.
- Provider and model selection come from environment configuration and approved
  tenant policy — never from visitor input, which would let a caller select an
  expensive model or one with different safety behavior.
- Usage accounting and tool-call shapes are normalized at the adapter boundary, so
  token counting and cost attribution do not vary by provider.

## Consequences

**Gained.** Evaluation runs are free, so they can be run on every commit without a
budget conversation. No API key is required to clone and run the project. Local
inference also forces the degraded-operation paths to be exercised routinely,
because a local server is genuinely less reliable than a hosted one.

**Cost — the significant one.** There is no public demo URL. A hosted instance
cannot reach a LAN model server, so a reader either runs the project locally or
watches a recording. For a portfolio artifact this is a real loss: a link that
works is worth more than a repository that must be cloned.

**Mitigation.** The provider interface makes a hosted adapter a configuration
change rather than a rewrite, so a deployed instance can flip to a hosted provider
without touching workflow code. Whether to do that is deferred to the packaging
stage, when the cost of a hosted eval budget can be measured rather than guessed.

**Cost.** Local models are weaker at instruction-following and tool-calling than
frontier hosted models. Prompts and tool schemas tuned against a local model may
behave differently against a hosted one, so the evaluation suite must be run
against any provider before it is trusted — this is exactly why `AI-001` requires
the same contract suite to pass against every configured adapter.

**Constraint.** Embeddings are served locally by `services/embedding`. The model
name is currently unpinned and loads with `trust_remote_code=True`, which executes
code from the model repository. `DEP-001` covers pinning the revision; the remote
code execution needs an explicit decision before any untrusted deployment.

## Alternatives considered

**Hosted provider as the default.** Rejected on evaluation cost, which is the
dominant model expense in a retrieval-focused project — evaluation dwarfs demo
traffic. Reconsider at packaging time if a public link is judged more valuable
than free iteration.

**Hosted for the deployed instance, local for development.** Not rejected —
deferred. This is the likely end state, and the provider interface exists so the
decision can be made late rather than now.

**Multiple hosted adapters up front.** Rejected for now. Building three adapters
before one workflow is finished optimizes for a capability claim rather than for
working software. The port exists; adapters are added when there is a reason.
