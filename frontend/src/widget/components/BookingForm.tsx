import { useRef, useState, type FormEvent } from "react";

import type { BookingOutcome } from "src/widget/useConversation";
import type { BookingContact } from "src/widget/types";

const FIELDS = [
  { name: "customerName", label: "Name", autoComplete: "name" },
  { name: "address", label: "Address", autoComplete: "street-address" },
  { name: "contact", label: "Phone or email", autoComplete: "email" }
] as const satisfies readonly { name: keyof BookingContact; label: string; autoComplete: string }[];

const CONSENT_REQUIRED = "Please agree to the data notice before booking.";

export interface BookingFormProps {
  service: string;
  slots: string[];
  /** Details from an earlier booking in this session (WCAG 2.2 §3.3.7). */
  remembered: BookingContact | null;
  consentStatement: string;
  onSubmit: (
    request: BookingContact & { service: string; slot: string }
  ) => Promise<BookingOutcome>;
}

function toTitle(text: string): string {
  return text.replace(/\w\S*/g, (word) => word.charAt(0).toUpperCase() + word.slice(1));
}

/** A form entry is a string or a File; every field here is a text input. */
function text(value: FormDataEntryValue | null): string {
  return typeof value === "string" ? value : "";
}

/**
 * The structured alternative to typing personal details into the chat.
 *
 * Contact details never leave the browser on a free-text turn: they are
 * collected here, behind an unticked consent box that names the tenant and what
 * is stored. The form stays on screen and editable when the backend rejects it.
 */
export function BookingForm({
  service,
  slots,
  remembered,
  consentStatement,
  onSubmit
}: BookingFormProps) {
  const [error, setError] = useState("");
  const [isSubmitting, setSubmitting] = useState(false);
  const [consentInvalid, setConsentInvalid] = useState(false);
  const consentRef = useRef<HTMLInputElement>(null);
  const submitRef = useRef<HTMLButtonElement>(null);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isSubmitting) return;

    const form = new FormData(event.currentTarget);
    if (!consentRef.current?.checked) {
      setError(CONSENT_REQUIRED);
      setConsentInvalid(true);
      consentRef.current?.focus();
      return;
    }

    setSubmitting(true);
    setError("");
    const outcome = await onSubmit({
      service,
      slot: text(form.get("slot")),
      customerName: text(form.get("customerName")),
      address: text(form.get("address")),
      contact: text(form.get("contact"))
    });

    if (!outcome.ok) {
      setSubmitting(false);
      setError(outcome.message ?? "");
      submitRef.current?.focus();
    }
    // A successful booking removes this form from the transcript, so there is
    // no state left here to restore.
  };

  return (
    <form
      className="booking-form-card"
      aria-labelledby="bookingTitle"
      onSubmit={(event) => void handleSubmit(event)}
    >
      <strong id="bookingTitle">{`Book ${toTitle(service)}`}</strong>

      <label htmlFor="bookingSlot">
        <span>Available slot</span>
        <select id="bookingSlot" name="slot" required defaultValue={slots[0]}>
          {slots.map((slot) => (
            <option key={slot} value={slot}>
              {slot}
            </option>
          ))}
        </select>
      </label>

      {FIELDS.map((field) => (
        <label key={field.name} htmlFor={`booking-${field.name}`}>
          <span>{field.label}</span>
          <input
            id={`booking-${field.name}`}
            name={field.name}
            type="text"
            autoComplete={field.autoComplete}
            required
            defaultValue={remembered?.[field.name] ?? ""}
          />
        </label>
      ))}

      <label className="consent-field" htmlFor="bookingConsent">
        <input
          ref={consentRef}
          id="bookingConsent"
          name="consent"
          type="checkbox"
          aria-describedby="bookingError"
          aria-invalid={consentInvalid || undefined}
          onChange={() => setConsentInvalid(false)}
        />
        <span>{consentStatement}</span>
      </label>

      <p className="form-error" id="bookingError" role="alert">
        {error}
      </p>

      <button ref={submitRef} type="submit" disabled={isSubmitting}>
        {isSubmitting ? "Booking…" : "Book selected slot"}
      </button>
    </form>
  );
}
