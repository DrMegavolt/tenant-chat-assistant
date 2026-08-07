import { useEffect, useState } from "react";

import type { AdminApi } from "src/admin/adminApi";
import type {
  KnowledgeDocument,
  KnowledgeFinding,
  KnowledgePreview,
  KnowledgeSource,
  KnowledgeVersion
} from "src/admin/knowledgeTypes";
import {
  canApprove,
  canPublish,
  KNOWLEDGE_FAULT_LABELS,
  KNOWLEDGE_INDEXING_LABELS,
  KNOWLEDGE_SOURCE_KIND_LABELS,
  KNOWLEDGE_STATE_LABELS
} from "src/admin/knowledgeTypes";
import { relativeTime } from "src/admin/time";
import { OUTCOME_LABELS } from "src/admin/traceTypes";
import type { TraceSearchRecord } from "src/admin/traceTypes";

export interface KnowledgeBaseProps {
  api: AdminApi;
  tenants: { tenantId: string; name: string }[];
  initialTenantId: string | null;
}

function relativeTimeOr(iso: string | null): string {
  if (!iso) return "";
  const seconds = new Date(iso).getTime() / 1000;
  return relativeTime(Number.isFinite(seconds) ? seconds : undefined);
}

function versionWindow(version: KnowledgeVersion): string {
  if (version.effectiveAt && version.expiresAt) {
    return `${relativeTimeOr(version.effectiveAt)} → ${relativeTimeOr(version.expiresAt)}`;
  }
  return version.effectiveAt ? `from ${relativeTimeOr(version.effectiveAt)}` : "";
}

/**
 * The FEAT-001 knowledge base: sources, documents, and versions with their
 * indexing status, chunk counts, errors, and last successful publish — plus
 * the tenant's bounded index-integrity findings, each linked to the source
 * version it affects and to the turns grounded in the affected generation
 * when the operator holds the trace-read grant.
 */
export function KnowledgeBase({ api, tenants, initialTenantId }: KnowledgeBaseProps) {
  const [tenantId, setTenantId] = useState(initialTenantId ?? tenants[0]?.tenantId ?? "");
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [findings, setFindings] = useState<KnowledgeFinding[]>([]);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [isLoading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [preview, setPreview] = useState<KnowledgePreview | null>(null);
  const [relatedTurns, setRelatedTurns] = useState<TraceSearchRecord[] | null>(null);
  const [relatedFor, setRelatedFor] = useState<KnowledgeFinding | null>(null);

  const run = async () => {
    setLoading(true);
    setError(null);
    try {
      const [tree, faults] = await Promise.all([
        api.knowledge(tenantId),
        api.knowledgeFindings(tenantId)
      ]);
      setSources(tree);
      setFindings(faults);
      setHasLoaded(true);
    } catch {
      setError("Could not reach the knowledge base. Retrying…");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    api
      .knowledge(tenantId)
      .then((tree) => {
        if (cancelled) return;
        setSources(tree);
        setHasLoaded(true);
      })
      .catch(() => {
        if (!cancelled) setError("Could not reach the knowledge base. Retrying…");
      });
    api
      .knowledgeFindings(tenantId)
      .then((faults) => {
        if (!cancelled) setFindings(faults);
      })
      .catch(() => {
        /* The tree is the primary surface; findings ride along. */
      });
    return () => {
      cancelled = true;
    };
  }, [api, tenantId]);

  const switchingTenant = (next: string) => {
    setTenantId(next);
    setSources([]);
    setFindings([]);
    setHasLoaded(false);
    setPreview(null);
    setRelatedTurns(null);
    setRelatedFor(null);
    void run();
  };

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setError(null);
    setNotice(null);
    try {
      await fn();
      await run();
      setNotice(`${label} complete.`);
    } catch {
      setError(`${label} failed.`);
    }
  };

  const approve = (version: KnowledgeVersion) => {
    void act(`Approving revision ${version.revision}`, () =>
      api.approveVersion(version.versionId, tenantId)
    );
  };

  const publish = (version: KnowledgeVersion) => {
    void act(`Publishing revision ${version.revision}`, () =>
      api.publishVersion(version.versionId, tenantId)
    );
  };

  const reindex = (version: KnowledgeVersion) => {
    void act(`Reindexing revision ${version.revision}`, () =>
      api.reindexVersion(version.versionId, tenantId)
    );
  };

  const expire = (version: KnowledgeVersion) => {
    void act(`Expiring revision ${version.revision}`, () =>
      api.expireVersion(version.versionId, tenantId)
    );
  };

  const remove = (document: KnowledgeDocument) => {
    const confirmed = window.confirm(`Delete “${document.title}” and every revision of it?`);
    if (confirmed) {
      void act(`Deleting “${document.title}”`, () =>
        api.deleteDocument(document.documentId, tenantId)
      );
    }
  };

  const toggleSource = (source: KnowledgeSource) => {
    void act(
      source.enabled ? `Disabling “${source.displayName}”` : `Enabling “${source.displayName}”`,
      () => api.setSourceEnabled(source.sourceId, tenantId, !source.enabled)
    );
  };

  const openPreview = (version: KnowledgeVersion) => {
    void (async () => {
      setError(null);
      try {
        const loaded = await api.previewVersion(version.versionId, tenantId);
        setPreview(loaded);
      } catch {
        setError("The preview could not be read.");
      }
    })();
  };

  const openRelated = (finding: KnowledgeFinding) => {
    void (async () => {
      setError(null);
      setRelatedFor(finding);
      setRelatedTurns(null);
      if (!finding.generationId) {
        setRelatedTurns([]);
        return;
      }
      try {
        const records = await api.searchTraces(tenantId, { generationId: finding.generationId });
        setRelatedTurns(records);
      } catch {
        setError("Related turns require the trace-read grant.");
        setRelatedTurns([]);
      }
    })();
  };

  const runIntegrityCheck = () => {
    void act("Running the integrity check", async () => {
      const faults = await api.runIntegrityCheck(tenantId);
      setFindings(faults);
    });
  };

  return (
    <section className="knowledge-base" aria-labelledby="knowledgeTitle">
      <div className="admin-panel-header">
        <h2 id="knowledgeTitle">Knowledge base</h2>
        <div className="review-toolbar">
          {tenants.length > 1 && (
            <label className="tenant-picker">
              <span className="visually-hidden">Tenant</span>
              <select
                value={tenantId}
                onChange={(event) => switchingTenant(event.target.value)}
                aria-label="Knowledge base tenant"
              >
                {tenants.map((tenant) => (
                  <option key={tenant.tenantId} value={tenant.tenantId}>
                    {tenant.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button
            type="button"
            className="ghost-button"
            disabled={isLoading}
            onClick={() => void run()}
          >
            {isLoading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {error && (
        <p className="admin-alert" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="muted-copy" role="status">
          {notice}
        </p>
      )}

      <FindingsPanel
        findings={findings}
        onRunCheck={runIntegrityCheck}
        relatedTurns={relatedTurns}
        relatedFor={relatedFor}
        onRelated={openRelated}
      />

      {hasLoaded && sources.length === 0 && (
        <div className="empty-state">
          <h3>No sources yet</h3>
          <p>Create a source, upload a document, approve, and publish it to make it answerable.</p>
          <CreateSource api={api} tenantId={tenantId} onCreated={() => void run()} />
        </div>
      )}

      {sources.map((source) => (
        <SourcePanel
          key={source.sourceId}
          api={api}
          tenantId={tenantId}
          source={source}
          onToggle={toggleSource}
          onApprove={approve}
          onPublish={publish}
          onReindex={reindex}
          onExpire={expire}
          onDelete={remove}
          onPreview={openPreview}
          onChanged={() => void run()}
        />
      ))}

      {preview && <PreviewPanel preview={preview} onClose={() => setPreview(null)} />}
    </section>
  );
}

function FindingsPanel({
  findings,
  onRunCheck,
  relatedTurns,
  relatedFor,
  onRelated
}: {
  findings: KnowledgeFinding[];
  onRunCheck: () => void;
  relatedTurns: TraceSearchRecord[] | null;
  relatedFor: KnowledgeFinding | null;
  onRelated: (finding: KnowledgeFinding) => void;
}) {
  return (
    <div className="kb-findings" aria-labelledby="findingsTitle">
      <div className="kb-panel-heading">
        <h3 id="findingsTitle">Index integrity</h3>
        <button type="button" className="ghost-button" onClick={onRunCheck}>
          Run integrity check
        </button>
      </div>
      {findings.length === 0 ? (
        <p className="muted-copy">
          No index-integrity findings. Indexing and the live index agree.
        </p>
      ) : (
        <ul className="kb-finding-list">
          {findings.map((finding) => (
            <li key={`${finding.versionId}-${finding.code}`} className="kb-finding">
              <strong>{KNOWLEDGE_FAULT_LABELS[finding.code] ?? finding.code}</strong>
              <span className="session-meta">
                {finding.documentTitle ?? "document"} · rev {finding.revision ?? "?"} ·{" "}
                {finding.sourceName ?? "source"} · {relativeTimeOr(finding.detectedAt)}
              </span>
              {finding.detail && Object.keys(finding.detail).length > 0 && (
                <span className="session-meta mono">{JSON.stringify(finding.detail)}</span>
              )}
              <button
                type="button"
                className="ghost-button"
                onClick={() => onRelated(finding)}
                disabled={relatedFor?.versionId === finding.versionId && relatedTurns === null}
              >
                Related turns
              </button>
              {relatedFor?.versionId === finding.versionId && relatedFor.code === finding.code && (
                <div className="kb-related" role="status">
                  {relatedTurns === null ? (
                    <span className="muted-copy">Loading…</span>
                  ) : relatedTurns.length === 0 ? (
                    <span className="muted-copy">No related turns.</span>
                  ) : (
                    <ul className="kb-related-list">
                      {relatedTurns.map((record) => (
                        <li key={record.turnId} className="session-meta mono">
                          {record.turnId.slice(0, 8)} ·{" "}
                          {OUTCOME_LABELS[record.outcome] ?? record.outcome}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SourcePanel({
  api,
  tenantId,
  source,
  onToggle,
  onApprove,
  onPublish,
  onReindex,
  onExpire,
  onDelete,
  onPreview,
  onChanged
}: {
  api: AdminApi;
  tenantId: string;
  source: KnowledgeSource;
  onToggle: (source: KnowledgeSource) => void;
  onApprove: (version: KnowledgeVersion) => void;
  onPublish: (version: KnowledgeVersion) => void;
  onReindex: (version: KnowledgeVersion) => void;
  onExpire: (version: KnowledgeVersion) => void;
  onDelete: (document: KnowledgeDocument) => void;
  onPreview: (version: KnowledgeVersion) => void;
  onChanged: () => void;
}) {
  return (
    <article className="kb-source" aria-label={source.displayName}>
      <div className="kb-panel-heading">
        <h3>{source.displayName}</h3>
        <div className="review-toolbar">
          <span className="session-meta">
            {source.domain} · {KNOWLEDGE_SOURCE_KIND_LABELS[source.kind] ?? source.kind} ·{" "}
            {source.enabled ? "enabled" : "disabled"}
          </span>
          <button type="button" className="ghost-button" onClick={() => onToggle(source)}>
            {source.enabled ? "Disable" : "Enable"}
          </button>
          <UploadForm api={api} tenantId={tenantId} source={source} onUploaded={onChanged} />
        </div>
      </div>

      {source.documents.length === 0 ? (
        <p className="muted-copy">No documents in this source yet.</p>
      ) : (
        <ul className="kb-document-list">
          {source.documents.map((document) => (
            <li key={document.documentId} className="kb-document">
              <div className="session-row">
                <strong>{document.title}</strong>
                <span className="session-meta mono">{document.externalKey}</span>
                {document.deleted && <span className="uncertain-chip">deleted</span>}
              </div>
              <ul className="kb-version-list">
                {document.versions.map((version) => (
                  <VersionRow
                    key={version.versionId}
                    version={version}
                    onApprove={onApprove}
                    onPublish={onPublish}
                    onReindex={onReindex}
                    onExpire={onExpire}
                    onDelete={onDelete}
                    onPreview={onPreview}
                    document={document}
                  />
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}

function VersionRow({
  version,
  document,
  onApprove,
  onPublish,
  onReindex,
  onExpire,
  onDelete,
  onPreview
}: {
  version: KnowledgeVersion;
  document: KnowledgeDocument;
  onApprove: (version: KnowledgeVersion) => void;
  onPublish: (version: KnowledgeVersion) => void;
  onReindex: (version: KnowledgeVersion) => void;
  onExpire: (version: KnowledgeVersion) => void;
  onDelete: (document: KnowledgeDocument) => void;
  onPreview: (version: KnowledgeVersion) => void;
}) {
  return (
    <li className="kb-version">
      <span className="session-row">
        <strong>rev {version.revision}</strong>
        <span className="session-meta">
          {KNOWLEDGE_STATE_LABELS[version.state] ?? version.state} ·{" "}
          {KNOWLEDGE_INDEXING_LABELS[version.indexingState] ?? version.indexingState}
        </span>
        <span className="session-meta mono">
          {version.chunkCount} chunks
          {version.embeddingModel ? ` · ${version.embeddingModel}` : ""}
        </span>
        <span className="session-meta">{versionWindow(version)}</span>
      </span>
      {version.indexErrorCode && (
        <span className="uncertain-chip">index error: {version.indexErrorCode}</span>
      )}
      {version.indexedAt && (
        <span className="session-meta">
          last successful publish {relativeTimeOr(version.indexedAt)}
        </span>
      )}
      <span className="kb-actions">
        <button type="button" className="ghost-button" onClick={() => onPreview(version)}>
          Preview
        </button>
        {canApprove(version) && (
          <button type="button" className="ghost-button" onClick={() => onApprove(version)}>
            Approve
          </button>
        )}
        {canPublish(version) && (
          <button type="button" className="ghost-button" onClick={() => onPublish(version)}>
            {version.state === "superseded" ? "Rollback to" : "Publish"}
          </button>
        )}
        {(version.state === "approved" ||
          version.state === "published" ||
          version.state === "superseded") && (
          <button type="button" className="ghost-button" onClick={() => onReindex(version)}>
            Reindex
          </button>
        )}
        {version.state === "published" && (
          <button type="button" className="ghost-button" onClick={() => onExpire(version)}>
            Expire now
          </button>
        )}
        {version === document.versions[document.versions.length - 1] && (
          <button type="button" className="ghost-button" onClick={() => onDelete(document)}>
            Delete document
          </button>
        )}
      </span>
    </li>
  );
}

function CreateSource({
  api,
  tenantId,
  onCreated
}: {
  api: AdminApi;
  tenantId: string;
  onCreated: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [displayName, setDisplayName] = useState("");
  const [domain, setDomain] = useState("financing");
  const [busy, setBusy] = useState(false);

  const create = async () => {
    setBusy(true);
    try {
      await api.createKnowledgeSource(tenantId, { domain, kind: "upload", displayName });
      setDisplayName("");
      setOpen(false);
      onCreated();
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button type="button" className="ghost-button" onClick={() => setOpen(true)}>
        New source
      </button>
    );
  }
  return (
    <form
      className="kb-inline-form"
      onSubmit={(event) => {
        event.preventDefault();
        void create();
      }}
    >
      <label className="visually-hidden" htmlFor="newSourceName">
        Source name
      </label>
      <input
        id="newSourceName"
        value={displayName}
        onChange={(event) => setDisplayName(event.target.value)}
        placeholder="Source name"
        required
      />
      <label className="visually-hidden" htmlFor="newSourceDomain">
        Domain
      </label>
      <input
        id="newSourceDomain"
        value={domain}
        onChange={(event) => setDomain(event.target.value)}
        placeholder="domain slug"
        pattern="[a-z][a-z0-9-]{1,62}"
        required
      />
      <button type="submit" className="ghost-button" disabled={busy || !displayName}>
        Create
      </button>
      <button type="button" className="ghost-button" onClick={() => setOpen(false)}>
        Cancel
      </button>
    </form>
  );
}

function UploadForm({
  api,
  tenantId,
  source,
  onUploaded
}: {
  api: AdminApi;
  tenantId: string;
  source: KnowledgeSource;
  onUploaded: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [externalKey, setExternalKey] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);

  const upload = async () => {
    if (!file) return;
    setBusy(true);
    try {
      await api.uploadKnowledge(tenantId, source.sourceId, file, {
        externalKey: externalKey || file.name,
        title: title || file.name
      });
      setFile(null);
      setTitle("");
      setExternalKey("");
      setOpen(false);
      onUploaded();
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button type="button" className="ghost-button" onClick={() => setOpen(true)}>
        Upload document
      </button>
    );
  }
  return (
    <form
      className="kb-inline-form"
      onSubmit={(event) => {
        event.preventDefault();
        void upload();
      }}
    >
      <label className="visually-hidden" htmlFor={`uploadFile-${source.sourceId}`}>
        Document file
      </label>
      <input
        id={`uploadFile-${source.sourceId}`}
        type="file"
        onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        required
      />
      <label className="visually-hidden" htmlFor={`uploadTitle-${source.sourceId}`}>
        Document title
      </label>
      <input
        id={`uploadTitle-${source.sourceId}`}
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        placeholder="Title"
      />
      <button type="submit" className="ghost-button" disabled={busy || !file}>
        Upload
      </button>
      <button type="button" className="ghost-button" onClick={() => setOpen(false)}>
        Cancel
      </button>
    </form>
  );
}

function PreviewPanel({ preview, onClose }: { preview: KnowledgePreview; onClose: () => void }) {
  return (
    <aside className="kb-preview" aria-label={`Preview of ${preview.title}`}>
      <div className="kb-panel-heading">
        <h3>Preview: {preview.title}</h3>
        <button type="button" className="ghost-button" onClick={onClose}>
          Close
        </button>
      </div>
      <p className="session-meta">
        {preview.mediaType} · {preview.parserVersion} · {preview.chunkCount} chunks
      </p>
      <ol className="kb-preview-blocks">
        {preview.blocks.map((block, index) => (
          <li key={`${block.location}-${index}`}>
            <span className="session-meta mono">{block.location}</span>
            <p className="kb-preview-text">{block.text}</p>
          </li>
        ))}
      </ol>
    </aside>
  );
}
