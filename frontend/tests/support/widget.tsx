import { fireEvent, render, waitFor } from "@testing-library/react";
import { expect } from "vitest";

import { DemoPage } from "src/demo/DemoPage";
import { tick } from "tests/support/timers";

export interface DemoOptions {
  companyId?: string;
  apiBaseUrl?: string;
  open?: boolean;
  /**
   * Wait for the widget to paint. Tests that install fake timers set this
   * false and drive the clock with `tick` instead, because `waitFor` polls on
   * a timer that will never fire.
   */
  awaitReady?: boolean;
}

/**
 * Render the demo host page with a mount element, exactly as `index.html`
 * declares it, and wait until the widget has painted its tenant branding.
 */
export async function renderDemo({
  companyId = "apex",
  apiBaseUrl,
  open = true,
  awaitReady = true
}: DemoOptions = {}): Promise<HTMLElement> {
  document.documentElement.lang = "en";
  document.title = "Tenant Chat Assistant Prototype";

  const host = document.createElement("div");
  host.id = "tenant-chat";
  host.dataset.companyId = companyId;
  if (apiBaseUrl !== undefined) host.dataset.apiBaseUrl = apiBaseUrl;
  if (open) host.dataset.open = "true";
  document.body.append(host);

  render(<DemoPage host={host} />);
  if (awaitReady) {
    await waitFor(() => {
      expect(host.shadowRoot?.querySelector("#chatCompany")?.textContent).toBeTruthy();
    });
  } else {
    await tick();
  }
  return host;
}

/** The widget's shadow root: the only surface a host page can observe. */
export function shadow(): ShadowRoot {
  const host = document.querySelector<HTMLElement>("#tenant-chat");
  if (!host?.shadowRoot) throw new Error("the widget is not mounted");
  return host.shadowRoot;
}

export function inWidget<T extends Element = HTMLElement>(selector: string): T | null {
  return shadow().querySelector<T>(selector);
}

/** Like `inWidget`, but fails the test rather than returning null. */
export function requireInWidget<T extends Element = HTMLElement>(selector: string): T {
  const found = inWidget<T>(selector);
  if (!found) throw new Error(`no element matches ${selector} inside the widget`);
  return found;
}

export function allInWidget<T extends Element = HTMLElement>(selector: string): T[] {
  return [...shadow().querySelectorAll<T>(selector)];
}

/** Type into the composer and send, the way a visitor does. */
export function submitChat(text: string): void {
  const input = requireInWidget<HTMLInputElement>("#chatInput");
  fireEvent.change(input, { target: { value: text } });
  fireEvent.submit(requireInWidget("#composer"));
}

export function selectTenant(name: string): void {
  const button = [...document.querySelectorAll("#tenantSwitcher button")].find(
    (candidate) => candidate.textContent === name
  );
  if (!button) throw new Error(`no preview company named ${name}`);
  fireEvent.click(button);
}

export function fillBooking(form: HTMLElement, { consent = true } = {}): void {
  const set = (id: string, value: string) => {
    fireEvent.change(form.querySelector(`#${id}`)!, { target: { value } });
  };
  set("booking-customerName", "Sam Lee");
  set("booking-address", "42 Cedar Road");
  set("booking-contact", "sam@example.test");
  if (consent) fireEvent.click(form.querySelector("#bookingConsent")!);
}

/** Drive one turn that ends with the booking form on screen. */
export async function openBookingForm(): Promise<HTMLElement> {
  selectTenant("Clearview Heating");
  submitChat("Show HVAC availability");
  await waitFor(() => expect(inWidget(".booking-form-card")).not.toBeNull());
  return requireInWidget(".booking-form-card");
}
