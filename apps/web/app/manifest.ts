import type { MetadataRoute } from "next";

/**
 * Next serves this at `/manifest.webmanifest` and links it from every page, so
 * the app becomes installable without adding a PWA framework: the platform
 * APIs already cover everything Phase 5.3A needs.
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Opportunity Radar AI",
    short_name: "Radar",
    description:
      "Veille d’opportunités : opportunités détectées, priorités et portfolio.",
    start_url: "/",
    scope: "/",
    display: "standalone",
    orientation: "portrait",
    lang: "fr",
    dir: "ltr",
    background_color: "#f4f7f5",
    theme_color: "#0b1220",
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      {
        src: "/icons/icon-maskable-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
  };
}
