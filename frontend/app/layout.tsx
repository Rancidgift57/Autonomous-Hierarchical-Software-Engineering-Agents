import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AHSEA Control Plane",
  description: "Mission control for the Autonomous Hierarchical Software Engineering Agent system.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        {/* Progressive enhancement only — the app looks correct on the
            system-font fallbacks in tailwind.config.ts even if this is
            blocked (e.g. offline dev, restricted network). */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="min-h-screen bg-base font-sans text-ink antialiased">{children}</body>
    </html>
  );
}
