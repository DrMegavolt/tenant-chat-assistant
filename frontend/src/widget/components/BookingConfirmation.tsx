import { useRef, useState } from "react";

import type { PendingBooking } from "src/widget/types";

export interface BookingConfirmationProps {
  pending: PendingBooking;
  /** The tenant-specific consent sentence shown before contact is submitted. */
  consentStatement: string;
  onDecide: (decision: "approved" | "declined") => Promise<void>;
}

const CONSENT_REQUIRED = "Please agree to the data notice before confirming.";

function toTitle(text: string): string {
  return text.replace(/\w\S*/g, (word) => word.charAt(0).toUpperCase() + word.slice(1));
}

/**
 * A booking the assistant proposed, awaiting the visitor's yes or no.
 *
 * Nothing is booked until the visitor approves. The details are echoed from
 * what will actually be booked (`PendingConfirmation`), not from what the
 * visitor believes they said. Approving submits the visitor's name, address,
 * and contact, so it is gated on the same consent the old contact form
 * required — contact details never leave the browser behind an unticked box.
 */
export function BookingConfirmation({
  pending,
  consentStatement,
  onDecide
}: BookingConfirmationProps) {
  const [isWorking, setWorking] = useState(false);
  const [consentInvalid, setConsentInvalid] = useState(false);
  const [error, setError] = useState("");
  const consentRef = useRef<HTMLInputElement>(null);

  const handle = async (decision: "approved" | "declined") => {
    if (decision === "approved" && !consentRef.current?.checked) {
      setConsentInvalid(true);
      setError(CONSENT_REQUIRED);
      consentRef.current?.focus();
      return;
    }
    if (isWorking) return;
    setWorking(true);
    try {
      await onDecide(decision);
      // A successful decision removes this confirmation from the transcript.
    } finally {
      // A failed decision leaves the card on screen with the visitor able to
      // retry or decline; without this the buttons stay disabled forever.
      setWorking(false);
    }
  };

  return (
    <div className="booking-confirmation-card" aria-labelledby="bookingConfirmTitle">
      <strong id="bookingConfirmTitle">Confirm your booking</strong>
      <dl className="booking-confirm-details">
        <div>
          <dt>Service</dt>
          <dd>{toTitle(pending.service)}</dd>
        </div>
        <div>
          <dt>Slot</dt>
          <dd>{pending.slot}</dd>
        </div>
        <div>
          <dt>Name</dt>
          <dd>{pending.customerName}</dd>
        </div>
        <div>
          <dt>Address</dt>
          <dd>{pending.address}</dd>
        </div>
      </dl>

      <label className="consent-field" htmlFor="bookingConfirmConsent">
        <input
          ref={consentRef}
          id="bookingConfirmConsent"
          name="consent"
          type="checkbox"
          aria-describedby="bookingConfirmError"
          aria-invalid={consentInvalid || undefined}
          onChange={() => {
            setConsentInvalid(false);
            setError("");
          }}
        />
        <span>{consentStatement}</span>
      </label>

      <p className="form-error" id="bookingConfirmError" role="alert">
        {error}
      </p>

      <div className="booking-confirm-actions">
        <button type="button" disabled={isWorking} onClick={() => void handle("approved")}>
          {isWorking ? "Confirming…" : "Confirm booking"}
        </button>
        <button type="button" disabled={isWorking} onClick={() => void handle("declined")}>
          Decline
        </button>
      </div>
    </div>
  );
}
