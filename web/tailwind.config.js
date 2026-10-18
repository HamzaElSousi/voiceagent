/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "media",
  theme: {
    extend: {
      fontFamily: { sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"] },
      colors: {
        brand: { 50: "#eef6ff", 100: "#d9eaff", 500: "#2f6fed", 600: "#2459c7", 700: "#1d47a0" },
      },
    },
  },
  plugins: [],
};
