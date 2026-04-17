import { useEffect, useState } from "react";

const PHRASES = [
  "thinking",
  "querying entities",
  "scanning registries",
  "checking states",
  "reviewing context",
  "analyzing topology",
  "searching memory",
  "reasoning",
  "building response",
  "evaluating options",
  "cross-referencing",
  "inspecting automations",
  "looking up devices",
  "processing",
  "reading config",
  "checking history",
  "considering approach",
  "formulating plan",
  "resolving references",
  "connecting the dots",
];

const INTERVAL_MS = 1800;

export function ThinkingIndicator() {
  const [index, setIndex] = useState(() =>
    Math.floor(Math.random() * PHRASES.length),
  );
  const [fade, setFade] = useState(true);

  useEffect(() => {
    const timer = setInterval(() => {
      setFade(false);
      setTimeout(() => {
        setIndex((i) => (i + 1) % PHRASES.length);
        setFade(true);
      }, 150);
    }, INTERVAL_MS);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="flex items-center gap-2 py-1">
      <PulsingDots />
      <span
        className="font-mono text-[10px] transition-opacity duration-150"
        style={{
          color: "var(--color-text-muted)",
          opacity: fade ? 1 : 0,
        }}
      >
        {PHRASES[index]}
      </span>
    </div>
  );
}

function PulsingDots() {
  return (
    <span className="inline-flex items-center gap-[3px]">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="inline-block h-[4px] w-[4px] rounded-full"
          style={{
            backgroundColor: "var(--color-accent)",
            animation: `pulse-dot 1.4s ease-in-out ${i * 0.2}s infinite`,
          }}
        />
      ))}
    </span>
  );
}
