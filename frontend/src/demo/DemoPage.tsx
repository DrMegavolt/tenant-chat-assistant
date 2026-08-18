import { useMemo, useState } from "react";

import { ChatApi, resolveApiBaseUrl } from "src/widget/api";
import type { TenantConfig } from "src/widget/types";
import { useTenants } from "src/widget/useTenants";
import { WidgetPortal } from "src/widget/WidgetPortal";
import { WidgetSurface } from "src/widget/WidgetSurface";

const DEFAULT_TENANT = "apex";

/** One row of the policy panel: what the tenant configuration permits. */
interface PolicyRow {
  term: string;
  detail: string;
  state?: "on" | "off";
}

function policyRows(config: TenantConfig): PolicyRow[] {
  return [
    {
      term: "Booking",
      detail: config.bookingEnabled
        ? "Allowed after service and slot confirmation."
        : "Disabled; route to the phone team.",
      state: config.bookingEnabled ? "on" : "off"
    },
    {
      term: "Lead capture",
      detail: config.leadCaptureEnabled
        ? "Enabled; the backend creates follow-up leads."
        : "Disabled for this company.",
      state: config.leadCaptureEnabled ? "on" : "off"
    },
    {
      term: "Proactive follow-up",
      detail: config.proactiveLeadCapture
        ? "Politely offers a callback once intent is clear."
        : "Never volunteers a callback.",
      state: config.proactiveLeadCapture ? "on" : "off"
    },
    { term: "Knowledge", detail: `${config.address} · ${config.hours}` },
    { term: "Service groups", detail: config.services.join(", ") }
  ];
}

/**
 * The page a reviewer opens: a plausible home-services site with the widget
 * embedded in it, and a switcher for the tenant policy it runs under.
 *
 * Everything visible here — copy, hours, services, what the assistant may do —
 * comes from the backend's tenant directory, so the page is evidence that a
 * policy change is a data change.
 */
export function DemoPage({ host }: { host: HTMLElement }) {
  const api = useMemo(() => new ChatApi(resolveApiBaseUrl(host)), [host]);
  const { tenants, error } = useTenants(api);
  const [tenantId, setTenantId] = useState(host.dataset.companyId ?? DEFAULT_TENANT);

  const config = tenants?.[tenantId] ?? null;
  const unknownTenant =
    tenants !== null && config === null ? `No chat is configured for “${tenantId}”.` : null;

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <div className="page">
        <aside className="preview-bar" aria-label="Demo preview">
          <p>
            <strong>Demo preview.</strong> This page stands in for a customer website. The same
            widget is embedded on both, with different policies and knowledge.
          </p>
          {tenants && (
            <div
              className="tenant-switcher"
              role="group"
              aria-label="Preview company"
              id="tenantSwitcher"
            >
              {Object.entries(tenants).map(([id, tenant]) => (
                <button
                  key={id}
                  type="button"
                  aria-pressed={id === tenantId}
                  onClick={() => setTenantId(id)}
                >
                  {tenant.name}
                </button>
              ))}
            </div>
          )}
        </aside>

        {config && (
          <>
            <header className="site-header">
              <span className="wordmark" id="siteName">
                {config.name}
              </span>
              <a className="call-link" href={`tel:${config.phone.replace(/[^+\d]/g, "")}`}>
                Call {config.phone}
              </a>
            </header>

            <main id="main">
              <section className="hero" aria-labelledby="headline">
                <div>
                  <p className="eyebrow">{config.hours}</p>
                  <h1 id="headline">{config.site.headline}</h1>
                  <p className="lede" id="description">
                    {config.site.description}
                  </p>
                  <ul className="service-chips">
                    {config.services.map((service) => (
                      <li key={service}>{service}</li>
                    ))}
                  </ul>
                </div>

                <dl className="contact-facts">
                  <div>
                    <dt>Phone</dt>
                    <dd>{config.phone}</dd>
                  </div>
                  <div>
                    <dt>Address</dt>
                    <dd>{config.address}</dd>
                  </div>
                  <div>
                    <dt>Hours</dt>
                    <dd>{config.hours}</dd>
                  </div>
                </dl>
              </section>

              <section className="policy-panel" aria-labelledby="policyTitle">
                <div>
                  <h2 id="policyTitle">Configured behaviour</h2>
                  <p>
                    The assistant in the corner is the same code on every site. These are the limits{" "}
                    {config.name} has set for it.
                  </p>
                </div>
                <dl className="policy-grid" id="configSummary">
                  {policyRows(config).map((row) => (
                    <div key={row.term}>
                      <dt>
                        {row.term}
                        {row.state && (
                          <span className={`policy-state ${row.state}`}>
                            {row.state === "on" ? "on" : "off"}
                          </span>
                        )}
                      </dt>
                      <dd>{row.detail}</dd>
                    </div>
                  ))}
                </dl>
              </section>
            </main>
          </>
        )}
      </div>

      <WidgetPortal host={host}>
        <WidgetSurface
          api={api}
          tenantId={tenantId}
          config={config}
          error={error ?? unknownTenant}
          defaultOpen={host.dataset.open === "true"}
        />
      </WidgetPortal>
    </>
  );
}
