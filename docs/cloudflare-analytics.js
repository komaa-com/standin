// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

// Cloudflare Web Analytics for docs.komaa.com.
//
// Mintlify's docs.json analytics integrations do not include Cloudflare, and
// there is no raw head-script field, so the beacon is injected here. Mintlify
// auto-includes .js files on every page. Google Analytics stays configured in
// docs.json (integrations.ga4) - this is additive.
(function () {
  if (typeof document === "undefined") return;
  var s = document.createElement("script");
  s.type = "module";
  s.src = "https://static.cloudflareinsights.com/beacon.min.js";
  s.setAttribute("data-cf-beacon", '{"token": "a85d76f41a064fe89445f454c30ad948"}');
  document.head.appendChild(s);
})();
