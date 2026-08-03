import type { ToolEvent } from "src/widget/types";

/** The prototype's visible record of what the model actually called. */
export function ToolTrace({ event }: { event: ToolEvent }) {
  return (
    <div className="tool-call">
      {`Tool: ${event.name}(${JSON.stringify(event.arguments)}) -> ${JSON.stringify(event.result)}`}
    </div>
  );
}
