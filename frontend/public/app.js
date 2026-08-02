/**
 * Demo-page entry point.
 *
 * The embeddable part of this page is `widget/widget.js`; everything here is
 * the stand-in customer website that hosts it and lets a reviewer switch
 * between tenant policies.
 */

import { mountWidget } from "./widget/embed.js";

const MOUNT_SELECTOR = "#tenant-chat";

async function init() {
  const mount = document.querySelector(MOUNT_SELECTOR);
  const { widget, tenants } = await mountWidget(mount);
  if (!tenants) return;

  const picker = document.querySelector("#tenantSelect");
  const selectTenant = (tenantId) => {
    mount.dataset.companyId = tenantId;
    widget.setTenant(tenantId, tenants[tenantId]);
    renderHostPage(tenants[tenantId]);
  };

  renderHostPage(tenants[mount.dataset.companyId]);
  picker.value = mount.dataset.companyId;
  picker.addEventListener("change", (event) => selectTenant(event.target.value));
}

function renderHostPage(config) {
  document.querySelector("#siteName").textContent = config.name;
  document.querySelector("#headline").textContent = config.site.headline;
  document.querySelector("#description").textContent = config.site.description;

  const entries = [
    ["Knowledge", `${config.address} · ${config.phone} · ${config.hours}`],
    [
      "Pricing",
      config.pricingPolicy === "never"
        ? "Never reveal pricing; route to phone."
        : "Allowed to quote fixed approved prices."
    ],
    [
      "Booking",
      config.bookingEnabled
        ? "Allowed after service and slot confirmation."
        : "Disabled; route to phone team."
    ],
    [
      "Lead capture",
      config.leadCaptureEnabled
        ? "Enabled; backend creates follow-up leads."
        : "Disabled for this company."
    ],
    [
      "Proactive",
      config.proactiveLeadCapture ? "Enabled; politely offers callback after intent." : "Disabled."
    ],
    ["Service groups", config.services.join(", ")]
  ];

  document.querySelector("#configSummary").replaceChildren(
    ...entries.map(([term, description]) => {
      const group = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = term;
      const dd = document.createElement("dd");
      dd.textContent = description;
      group.append(dt, dd);
      return group;
    })
  );
}

init();
