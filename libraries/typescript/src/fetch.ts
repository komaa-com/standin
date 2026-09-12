// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Fetching a URL an agent chose, without fetching your own infrastructure.
 *
 * An agent that can show the caller a picture will sooner or later be handed a
 * URL by its own model, and that model is steered by whoever is on the call. So
 * the URL is untrusted input wearing a trusted costume, and a naive GET of it is
 * a server-side request forgery: `169.254.169.254` is cloud credentials,
 * `127.0.0.1` is whatever else you run, and `10.0.0.0/8` is the rest of your
 * network.
 *
 * {@link fetchPublicImage} is the guarded way to do it: http and https only, no
 * embedded credentials, and no host that resolves into private, loopback,
 * link-local or reserved space.
 *
 * The subtle half is the rebind. Validating a hostname resolves it once, and the
 * resolution the HTTP stack does a moment later can answer differently, so the
 * request is made through a guarded `lookup` that re-checks the address the
 * socket will actually connect to. One redirect hop is followed, because image
 * CDNs habitually redirect to the real asset, and the target goes through the
 * whole guard again rather than being trusted for having come from a host that
 * passed.
 *
 * Identical in shape to the Python SDK's `standin.fetch`.
 */

import { lookup } from "node:dns/promises";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { isIP } from "node:net";

/** Resolves a hostname to addresses. Replaceable so tests can drive a rebind. */
export type LookupFn = (
  hostname: string,
  opts: { all: true; verbatim: true },
) => Promise<Array<{ address: string; family: number }>>;

/** Decides whether an address may be fetched. Replaceable for tests. */
export type ForbiddenIpFn = (ip: string) => boolean;

const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);

function ipv4ToInt(ip: string): number {
  return (
    ip.split(".").reduce((acc, octet) => (acc << 8) + Number(octet), 0) >>> 0
  );
}

function inCidr4(ip: number, base: string, maskBits: number): boolean {
  const mask = maskBits === 0 ? 0 : (~0 << (32 - maskBits)) >>> 0;
  return (ip & mask) === (ipv4ToInt(base) & mask);
}

/** True for any IPv4 address that must never be fetched server-side. */
export function isForbiddenIpv4(ip: string): boolean {
  const n = ipv4ToInt(ip);
  return (
    inCidr4(n, "0.0.0.0", 8) || // "this" network
    inCidr4(n, "10.0.0.0", 8) || // RFC1918
    inCidr4(n, "100.64.0.0", 10) || // carrier-grade NAT
    inCidr4(n, "127.0.0.0", 8) || // loopback
    inCidr4(n, "169.254.0.0", 16) || // link-local, including cloud metadata
    inCidr4(n, "172.16.0.0", 12) || // RFC1918
    inCidr4(n, "192.0.0.0", 24) || // IETF protocol assignments
    inCidr4(n, "192.168.0.0", 16) || // RFC1918
    inCidr4(n, "198.18.0.0", 15) || // benchmarking
    inCidr4(n, "224.0.0.0", 3) // multicast, reserved and broadcast
  );
}

/** True for any IPv6 address that must never be fetched server-side. */
export function isForbiddenIpv6(ip: string): boolean {
  const lower = ip.toLowerCase();
  // A v4 address wearing a v6 spelling is still that v4 address, so judge what
  // it embeds. Otherwise the whole v4 table above is one notation away from
  // being bypassed.
  if (lower.startsWith("::ffff:") || lower.startsWith("64:ff9b:")) {
    const dotted = lower.match(/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$/);
    if (dotted?.[1]) return isForbiddenIpv4(dotted[1]);
    const groups = (lower.split("::").pop() ?? "").split(":").filter(Boolean);
    if (groups.length <= 2) {
      const hi = groups.length === 2 ? parseInt(groups[0] ?? "0", 16) : 0;
      const lo = parseInt(groups[groups.length - 1] ?? "0", 16) || 0;
      return isForbiddenIpv4(`${hi >> 8}.${hi & 0xff}.${lo >> 8}.${lo & 0xff}`);
    }
    return true; // a malformed mapped form: refuse rather than guess
  }
  if (lower === "::" || lower === "::1") return true;
  const firstGroup = lower.split(":")[0] || "0";
  const first16 = parseInt(firstGroup === "" ? "0" : firstGroup, 16);
  if ((first16 & 0xfe00) === 0xfc00) return true; // fc00::/7 unique-local
  if ((first16 & 0xffc0) === 0xfe80) return true; // fe80::/10 link-local
  return false;
}

/**
 * True for any address that must never be fetched server-side.
 *
 * Unparseable input is forbidden too: something that is not an address cannot be
 * proven safe, and failing closed is the only correct direction here.
 */
export function isForbiddenIp(ip: string): boolean {
  const family = isIP(ip);
  if (family === 4) return isForbiddenIpv4(ip);
  if (family === 6) return isForbiddenIpv6(ip);
  return true;
}

/**
 * Check a URL is safe to fetch, or throw saying why.
 *
 * http and https only, no embedded credentials, and every address the host
 * resolves to must be public.
 */
export async function assertPublicHttpUrl(
  raw: string,
  lookupFn: LookupFn = lookup as LookupFn,
  isForbiddenFn: ForbiddenIpFn = isForbiddenIp,
): Promise<URL> {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("not a valid URL");
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error(`forbidden protocol ${url.protocol}`);
  }
  if (url.username || url.password) {
    throw new Error("URLs with embedded credentials are not allowed");
  }
  // The WHATWG URL keeps the brackets on an IPv6 literal, which isIP rejects.
  const host = url.hostname.replace(/^\[|\]$/g, "");
  if (isIP(host)) {
    if (isForbiddenFn(host))
      throw new Error(`address ${host} is private or reserved`);
    return url;
  }
  let addrs: Array<{ address: string; family: number }>;
  try {
    addrs = await lookupFn(host, { all: true, verbatim: true });
  } catch {
    throw new Error(`cannot resolve host ${host}`);
  }
  if (addrs.length === 0)
    throw new Error(`host ${host} resolves to no addresses`);
  for (const addr of addrs) {
    if (isForbiddenFn(addr.address)) {
      throw new Error(
        `host ${host} resolves to private or reserved address ${addr.address}`,
      );
    }
  }
  return url;
}

/**
 * Fetch an image from an untrusted URL.
 *
 * Bounded in every direction that matters: total time, declared length, streamed
 * length, and one redirect hop. Throws for anything that fails, so a caller can
 * hand the reason straight back to the agent that supplied the URL.
 */
export async function fetchPublicImage(
  rawUrl: string,
  maxBytes: number,
  timeoutMs = 10_000,
  lookupFn: LookupFn = lookup as LookupFn,
  isForbiddenFn: ForbiddenIpFn = isForbiddenIp,
  redirectsLeft = 1,
): Promise<{ bytes: Buffer; mime: string }> {
  const url = await assertPublicHttpUrl(rawUrl, lookupFn, isForbiddenFn);

  // The HTTP stack calls this to resolve the host, so an answer that was public
  // during validation and private a moment later is refused here rather than
  // quietly connecting to something internal.
  const guardedLookup = (
    hostname: string,
    options: { all?: boolean },
    cb: (
      err: NodeJS.ErrnoException | null,
      address: unknown,
      family?: number,
    ) => void,
  ): void => {
    lookupFn(hostname, { all: true, verbatim: true }).then(
      (addrs) => {
        const bad = addrs.find((a) => isForbiddenFn(a.address));
        if (bad || addrs.length === 0) {
          cb(
            new Error(
              `DNS rebind blocked: ${hostname} resolved to ${bad?.address ?? "nothing"}`,
            ),
            null as never,
          );
          return;
        }
        if (options.all) {
          cb(null, addrs);
          return;
        }
        const first = addrs[0];
        if (first === undefined) {
          cb(new Error(`${hostname} resolved to nothing`), null as never);
          return;
        }
        cb(null, first.address, first.family);
      },
      (err) => cb(err as NodeJS.ErrnoException, null as never),
    );
  };

  return new Promise((resolve, reject) => {
    const req = (url.protocol === "https:" ? httpsRequest : httpRequest)(
      url,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { lookup: guardedLookup as any, headers: { accept: "image/*" } },
      (res) => {
        if (REDIRECT_STATUSES.has(res.statusCode ?? 0)) {
          const location = res.headers.location;
          res.resume();
          if (!location) {
            reject(
              new Error(
                `fetch ${rawUrl} returned HTTP ${res.statusCode} with no Location`,
              ),
            );
            return;
          }
          if (redirectsLeft <= 0) {
            reject(new Error(`fetch ${rawUrl} followed too many redirects`));
            return;
          }
          let next: string;
          try {
            next = new URL(location, url).toString();
          } catch {
            reject(
              new Error(
                `fetch ${rawUrl} returned an invalid redirect location`,
              ),
            );
            return;
          }
          // Resolved against the CURRENT url, then put through the whole guard
          // again. A target is not trustworthy for having been named by a host
          // that passed.
          fetchPublicImage(
            next,
            maxBytes,
            timeoutMs,
            lookupFn,
            isForbiddenFn,
            redirectsLeft - 1,
          ).then(resolve, reject);
          return;
        }
        if (res.statusCode !== 200) {
          res.resume();
          reject(new Error(`fetch ${rawUrl} returned HTTP ${res.statusCode}`));
          return;
        }
        const declared = Number(res.headers["content-length"] ?? NaN);
        if (Number.isFinite(declared) && declared > maxBytes) {
          res.destroy();
          reject(
            new Error(
              `response too large (${declared} bytes, max ${maxBytes})`,
            ),
          );
          return;
        }
        const mime =
          (res.headers["content-type"] ?? "image/jpeg").split(";")[0]?.trim() ??
          "image/jpeg";
        const chunks: Buffer[] = [];
        let total = 0;
        res.on("data", (chunk: Buffer) => {
          // The declared length is a claim, not a promise. Counting what
          // actually arrives is what bounds a lying or chunked response.
          total += chunk.length;
          if (total > maxBytes) {
            res.destroy();
            reject(
              new Error(`response exceeded ${maxBytes} bytes; aborting read`),
            );
            return;
          }
          chunks.push(chunk);
        });
        res.on("end", () => resolve({ bytes: Buffer.concat(chunks), mime }));
        res.on("error", reject);
      },
    );
    // A hard total deadline: the socket's own timeout option is only an idle
    // timer, so a slow drip would never trip it.
    const deadline = setTimeout(
      () =>
        req.destroy(
          new Error(`fetch ${rawUrl} timed out after ${timeoutMs}ms`),
        ),
      timeoutMs,
    );
    deadline.unref?.();
    req.on("error", (err) => {
      clearTimeout(deadline);
      reject(err);
    });
    req.on("close", () => clearTimeout(deadline));
    req.end();
  });
}
