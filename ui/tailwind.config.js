/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Match HA's default dark theme palette well enough that we don't
        // clash when embedded as a panel iframe.
        surface: "#12141a",
        elevated: "#1b1f28",
        border: "#2a2f3a",
        mute: "#8a93a6",
      },
    },
  },
  plugins: [],
};
