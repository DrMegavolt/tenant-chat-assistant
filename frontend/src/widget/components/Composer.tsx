import { useState, type FormEvent, type RefObject } from "react";

import { SendIcon } from "src/widget/icons";

export interface ComposerProps {
  inputRef: RefObject<HTMLInputElement | null>;
  isSending: boolean;
  onSend: (text: string) => void;
}

export function Composer({ inputRef, isSending, onSend }: ComposerProps) {
  const [value, setValue] = useState("");

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const text = value.trim();
    if (!text) return;
    setValue("");
    onSend(text);
  };

  return (
    <form className="composer" id="composer" onSubmit={handleSubmit}>
      <label className="visually-hidden" htmlFor="chatInput">
        Message
      </label>
      <input
        ref={inputRef}
        id="chatInput"
        name="message"
        type="text"
        autoComplete="off"
        aria-describedby="privacyNote"
        placeholder="Ask a question…"
        disabled={isSending}
        value={value}
        onChange={(event) => setValue(event.target.value)}
      />
      <button type="submit" disabled={isSending || !value.trim()}>
        <SendIcon />
        <span className="visually-hidden">Send message</span>
      </button>
    </form>
  );
}
