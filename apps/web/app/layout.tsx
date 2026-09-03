import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Opportunity Radar AI",
  description: "Opportunity Radar AI project foundation",
  applicationName: "Opportunity Radar AI",
  // `app/manifest.ts` is served at this path and linked automatically; naming
  // it here keeps the link explicit alongside the rest of the PWA metadata.
  manifest: "/manifest.webmanifest",
  appleWebApp: { capable: true, title: "Radar", statusBarStyle: "default" },
  icons: {
    icon: [
      { url: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
      { url: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
    apple: [{ url: "/icons/icon-192.png", sizes: "192x192", type: "image/png" }],
  },
};

export const viewport: Viewport = {
  themeColor: "#0b1220",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
