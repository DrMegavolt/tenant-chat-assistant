/**
 * The FEAT-001 knowledge base's data contracts.
 *
 * The tree surface is content-free by contract: titles, states, counts, and
 * timestamps only. Content arrives only through the bounded preview surface,
 * which parses the operator's own stored document.
 */

/** One version as the operator reads it, with its index generation merged. */
export interface KnowledgeVersion {
  versionId: string;
  revision: number;
  state: string;
  indexingState: string;
  safetyState: string;
  visibility: string;
  checksum: string;
  byteSize: number;
  mediaType: string;
  approvedAt: string | null;
  publishedAt: string | null;
  supersededAt: string | null;
  indexedAt: string | null;
  effectiveAt: string | null;
  expiresAt: string | null;
  indexErrorCode: string | null;
  generationStatus: string | null;
  chunkCount: number;
  embeddingModel: string | null;
}

/** One document and every revision of it. */
export interface KnowledgeDocument {
  documentId: string;
  sourceId: string;
  externalKey: string;
  title: string;
  deleted: boolean;
  versions: KnowledgeVersion[];
}

/** One tenant-owned origin that documents belong to. */
export interface KnowledgeSource {
  sourceId: string;
  tenantId: string;
  domain: string;
  kind: string;
  displayName: string;
  enabled: boolean;
  documents: KnowledgeDocument[];
}

/** The bounded preview of one version's parsed content. */
export interface KnowledgePreview {
  versionId: string;
  documentId: string;
  title: string;
  mediaType: string;
  parserVersion: string;
  chunkCount: number;
  blocks: { location: string; text: string }[];
}

/** One content-free index-integrity finding, linked to its source version. */
export interface KnowledgeFinding {
  code: string;
  tenantId: string;
  documentId: string;
  versionId: string;
  generationId: string | null;
  detectedAt: string;
  detail: Record<string, unknown>;
  sourceName: string | null;
  documentTitle: string | null;
  revision: number | null;
}

export const KNOWLEDGE_STATE_LABELS: Record<string, string> = {
  draft: "Draft",
  approved: "Approved",
  published: "Published",
  superseded: "Superseded",
  deleted: "Deleted"
};

export const KNOWLEDGE_INDEXING_LABELS: Record<string, string> = {
  pending: "Awaiting index",
  indexing: "Indexing",
  indexed: "Indexed",
  failed: "Index failed"
};

export const KNOWLEDGE_FAULT_LABELS: Record<string, string> = {
  index_missing_generation: "Missing index generation",
  index_partial_generation: "Partially indexed",
  index_chunk_count_mismatch: "Chunk count mismatch",
  index_embedding_model_mismatch: "Embedding model mismatch",
  index_lag: "Excessive index lag",
  index_superseded_retrievable: "Superseded content still retrievable"
};

export const KNOWLEDGE_SOURCE_KIND_LABELS: Record<string, string> = {
  upload: "Upload",
  url: "URL",
  manual: "Manual"
};

/** A version that can be published right now (approved, published, or
 * superseded — publishing a superseded version is a rollback). */
export function canPublish(version: KnowledgeVersion): boolean {
  return version.state === "approved" || version.state === "superseded";
}

/** A version that can be approved: only a draft. */
export function canApprove(version: KnowledgeVersion): boolean {
  return version.state === "draft";
}
