import type { NextConfig } from "next";
import { buildNextSecurityHeaders } from "./lib/security-headers";

const nextConfig: NextConfig = {
  async headers() {
    return buildNextSecurityHeaders();
  },
};

export default nextConfig;
