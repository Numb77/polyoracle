/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Standalone output required for Docker — produces a self-contained build
  output: process.env.DOCKER_BUILD ? "standalone" : undefined,
};

module.exports = nextConfig;
