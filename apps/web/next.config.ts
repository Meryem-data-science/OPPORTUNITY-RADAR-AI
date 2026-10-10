import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  webpack(config) {
    // No dependency symlinks are used by this app. Avoid native Windows
    // canonicalization, which can be unavailable in a restricted workspace.
    config.resolve.symlinks = false;
    config.resolve.alias = { ...config.resolve.alias, "@": process.cwd() };
    return config;
  },
};

export default nextConfig;