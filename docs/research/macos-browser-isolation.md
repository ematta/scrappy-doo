# Network isolation for Playwright MCP on macOS

Research date: 2026-11-08. This is a design assessment based on vendor documentation, not a test of an implementation or a guarantee against a compromised host, hypervisor, Docker daemon, proxy, or kernel.

## Partial local network check

On 2026-11-11, a preliminary check used OrbStack 29.8.2 and the local
`nginx:alpine` image with image ID
`sha256:ef9a6e2b45c23a9693064eb858517632288d5da63bcb9f86c7cef4983290f41e`.
A container on Docker's normal bridge could open a TCP connection to a separate
nginx receiver on that bridge. A container attached only to a newly created
internal network could not open the same connection. The containers and network
were removed after the check.

This only checks one direct TCP path between containers. It did not exercise a
browser, proxy, HTTPS inspection, independent connection logging, DNS, UDP,
host services, policy changes, a viewer, or manual sign-in. It does not verify
the candidate topology or establish strict isolation. OrbStack has not been
established as equivalent to Docker Desktop for the remaining checks.

The candidate browser build uses the v1.65.0-noble image index digest published
in the [Microsoft Artifact Registry](https://mcr.microsoft.com/product/playwright)
and pins [Playwright MCP 0.0.85](https://github.com/microsoft/playwright-mcp/blob/v0.0.85/package.json).
Its manifest records the Chromium version and revision expected by that MCP
dependency, and the image build checks those values. This is a candidate build,
not evidence of a working browser, a protected viewer, or network isolation.
On 2026-11-11 that image built on OrbStack 29.8.2, but a network-disabled
Chromium startup with a non-root user, all capabilities dropped,
no-new-privileges, and Chromium sandboxing enabled failed with “No usable
sandbox.” A second attempt using Playwright's versioned seccomp profile failed
inside the OCI runtime with a segmentation fault before Chromium started. The
temporary diagnostic containers were removed. These observations do not
establish whether a different supported runtime or configuration would work.

Further checks on the same OrbStack runtime found that the kernel reports a
nonzero user-namespace limit, but `unshare -Ur true` inside a non-root,
network-disabled, capability-dropped container with no-new-privileges returns
`Operation not permitted`. The Playwright seccomp-profile attempt produced an
OCI runtime segmentation fault with a simple nginx image as well as with the
browser image; the other configured runtime name produced the same result. The
existing Docker Desktop context did not have a running daemon. These checks do
not establish the root cause of the OCI crash. The Docker adapter now refuses
to create a browser container unless an actual sandboxed Chromium launch under
the same basic restrictions succeeds; this check is not a network-isolation
proof.

The candidate Docker adapter uses a pinned profile based on the [Playwright
v1.65.0 seccomp profile](https://github.com/microsoft/playwright/blob/v1.65.0/utils/docker/seccomp_profile.json)
for both the sandbox startup check and browser container. The only added rule
allows the `chroot` syscall: Chromium uses it inside its namespace sandbox,
and the original profile permits it only when the container has `CAP_SYS_CHROOT`.
The kernel still checks the required capability; the container receives no
added capabilities. The adapter verifies the profile's SHA-256 before use and
refuses to start if it is missing or changed. This does not establish the
network boundary.

On 2026-11-11, Docker Desktop 29.9.3 launched headed Chromium 158.0.8127.18
with this profile, networking disabled, all container capabilities dropped,
no-new-privileges, and Chromium sandboxing enabled. The browser rendered a
local test page. This verifies that launch on the tested runtime, not the
proxy, viewer, or strict network isolation.

## Recommendation

For a dynamically updated hostname allowlist, run a dedicated Playwright MCP server and browser in a Linux container on Docker Desktop for Mac, attached **only** to a dedicated Docker `internal` network. Put a separate, trusted egress proxy on that network and a second external network. Configure the browser to use the proxy for all supported traffic. Restrict the proxy to explicit approved DNS names and ports, and deny everything else. The browser container must have no Docker socket, no host networking, no additional network attachments, no privilege or network-administration capabilities, and no shared host credentials or unnecessary mounts. Use MCP stdio or a separately controlled management channel; do not expose the proxy's management interface to the browser network. Docker documents that an internal network has no default route and drops traffic to other networks, but also that its gateway and configured host services can remain reachable; explicitly test and restrict those paths. [Docker internal networks](https://docs.docker.com/reference/cli/docker/network/create/), [Docker security](https://docs.docker.com/engine/security/), [Docker Desktop networking](https://docs.docker.com/desktop/features/networking/).

This topology is a proposed enforcement design, not an out-of-the-box Docker feature: implement and verify the gateway/host-service restrictions in the actual environment. If those paths cannot be closed, or no trusted proxy and isolated control channel can be established, do not claim strict isolation; use a dedicated VM with enforceable egress rules or do not run the browser. Docker Desktop funnels container networking through host backend processes, so a host firewall that identifies only the browser executable is not a substitute for container-scoped enforcement. [Docker Desktop networking](https://docs.docker.com/desktop/features/networking/).

## Policy and limitations

- Apply an exact, normalized hostname and destination port allowlist to **each** HTTP request and HTTPS CONNECT, including redirects, frames, scripts, images, fetches and WebSocket handshakes. A dependency must be explicitly allowed; redirects to another host must be rejected unless that host is allowed. Treat scheme, port, IDNA, case, trailing dots, IP literals and subdomains deliberately; `example.com` must not accidentally allow `notexample.com`. Default deny malformed or ambiguous inputs. The proxy must resolve the hostname itself, reject loopback, link-local, private, metadata, gateway and other prohibited IP addresses for both address families except for explicitly authorized private targets described below, and connect to the checked address without a second DNS lookup. Recheck each new connection to limit DNS rebinding. Never allow the browser to reach arbitrary external DNS or DNS-over-HTTPS directly.
- An HTTPS CONNECT proxy enforces the requested authority, not all encrypted HTTP requests inside the tunnel. Shared IPs, HTTP/2 connection coalescing, domain fronting, malicious allowed servers and encrypted traffic can defeat an assertion that every requested URL host is independently checked. A proxy that merely inspects SNI has similar identity and encryption limitations. If the requirement is exact per-request origin isolation against adversarial sites, a tunnel alone is insufficient: evaluate a purpose-built TLS-intercepting proxy with managed trust and per-request enforcement, and test browser/protocol behavior. Even then, an authorized server can relay information elsewhere, so host allowlisting cannot guarantee where data ultimately travels.
- Treat updates as privileged policy changes: authenticate and authorize the controller, validate and atomically publish the new rules, and default deny if configuration is missing, stale, or unavailable. Revocation must close existing matching tunnels and pooled connections or restart the proxy/browser, otherwise a previously allowed connection can continue. Control the update API independently of page content and the browser process. Avoid broad wildcards; record grants, changes and denied attempts without logging secrets.
- Fail closed if the proxy stops: no direct alternative route, external DNS, UDP/QUIC, raw sockets, proxy bypass or fallback. This is why proxy configuration alone is inadequate. Also test local loopback and Docker gateway access; the browser can reach its own loopback, so do not run sensitive local listeners there. Do not join a normal bridge or host network, grant `NET_ADMIN`, use privileged mode, or mount the Docker socket. Protect the MCP transport and disable unneeded code-execution/file access capabilities. These are implementation requirements, not claims that they have been validated here.

## Why built-in options are insufficient

Microsoft explicitly says Playwright MCP's `--allowed-origins` and `--blocked-origins` are **not security boundaries** and do not affect redirects; `--allowed-hosts` controls access to the MCP server, not outbound browser destinations. Playwright also documents that request routing sees the original redirect request, while the browser follows later redirects without rerunning the handler, and service workers can hide requests from normal route interception. Blocking service workers and enabling origin rules are useful defense in depth, not the egress boundary. [Playwright MCP configuration](https://github.com/microsoft/playwright-mcp/blob/main/README.md), [Playwright network routing](https://playwright.dev/docs/network).

The built-in macOS application firewall is documented for incoming connections; it is not an outbound domain allowlist. Apple recommends Network Extension content-filter providers for content filtering and warns against using packet tunnels for that purpose; Apple also advises avoiding direct Packet Filter or routing-table changes for VPN apps because of compatibility conflicts. [macOS firewall](https://support.apple.com/en-us/guide/security/seca0e83763f/web), [Apple TN3120](https://developer.apple.com/documentation/technotes/tn3120-expected-use-cases-for-network-extension-packet-tunnel-providers), [WWDC Network Extension](https://developer.apple.com/videos/play/wwdc2025/234/).

A native macOS alternative is a signed Network Extension system extension using `NEFilterDataProvider` (and potentially `NEFilterPacketProvider`) with default-deny flow verdicts and a protected policy controller. Apple provides source-process audit tokens and a new-flow decision callback; socket filtering must be enabled explicitly because its default is false. This requires developer entitlements, extension installation and lifecycle handling, and reliable identification of the entire browser process tree, including helpers. Flow metadata, DNS, shared IPs, encrypted traffic, process attribution, revocation and extension failure behavior all require implementation-specific verification; a flow-level filter alone does not prove per-request hostname enforcement. [Deployment requirements](https://developer.apple.com/documentation/technotes/tn3134-network-extension-provider-deployment), [entitlements](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.networking.networkextension), [socket filtering](https://developer.apple.com/documentation/networkextension/nefilterproviderconfiguration/filtersockets), [source process identity](https://developer.apple.com/documentation/networkextension/nefilterflow/sourceappaudittoken), [flow decisions](https://developer.apple.com/documentation/networkextension/nefilterdataprovider/handlenewflow(_:)).

Apple's macOS 26 URL filter checks WebKit and URLSession requests, but other browsers must voluntarily adopt its participation API; it is thus not established as enforcement for arbitrary Playwright Chromium or Firefox. [Apple URL filters](https://developer.apple.com/documentation/networkextension/url-filters).

## Acceptance checks

Test the actual pinned macOS, Docker Desktop, browser, MCP and proxy versions. Observe both proxy verdicts and a controlled receiving server, not just browser errors. Confirm an approved site works and unapproved direct navigations, redirect hops, iframe/image/script/fetch requests and WebSockets do not reach their targets. Exercise both IPv4 and IPv6, IP literals, DNS rebinding, alternate ports, subdomain tricks, malformed authorities, HTTP/2 reuse, service workers and UDP/QUIC. Attempt direct TCP, UDP, DNS, DoH, host-gateway, loopback, other-container and proxy-management access from the browser container; verify no unexpected egress even when the proxy crashes, its policy fails to load, or the browser changes proxy settings. Test policy add, remove and revocation of existing connections, browser restart and Docker restart. Fail the deployment if any path violates the stated policy. Passing tests demonstrates the tested configuration and threat cases only, not a general guarantee.

## Concrete candidate and unresolved integration

A candidate for a prototype is a custom Linux Playwright MCP image on Docker Desktop, using a headed Chromium and a separate `mitmdump` regular-mode proxy with a custom default-deny policy addon. The MCP server has a `--proxy-server` option; mitmproxy documents both a CONNECT hook and a separate HTTP request-headers hook for requests inside a tunnel. The addon must validate the effective scheme, host and port on **every** request, compare it consistently with CONNECT/TLS identity, reject ambiguous or unsupported protocols, and enforce the address and revocation requirements above. Hooks are building blocks, not a supplied allowlist implementation or proof that policy is enforced before all upstream connections: in particular, mitmproxy's default upstream certificate sniffing can connect before an HTTP request hook. Validate and control that connection path as well. [MCP configuration](https://github.com/microsoft/playwright-mcp/blob/main/README.md), [mitmproxy event hooks](https://docs.mitmproxy.org/stable/api/events.html), [certificate sniffing](https://docs.mitmproxy.org/stable/concepts/certificates/).

Mitmproxy creates its own interception CA. Put only its public certificate in the isolated browser user's Linux Chromium NSS database; keep the CA private key solely in the proxy, and do not install it in the macOS keychain. Chromium documents the NSS database path change in M146, so use the path for the pinned browser version. Keep upstream certificate verification enabled and configure any required private-origin CAs separately in the proxy; do not use `--ignore-https-errors` as a substitute. Certificate pinning may prevent interception. [Chromium Linux certificate management](https://chromium.googlesource.com/chromium/src/+/master/docs/linux/cert_management.md), [mitmproxy certificates](https://docs.mitmproxy.org/stable/concepts/certificates/), [mitmproxy options](https://docs.mitmproxy.org/stable/concepts/options/).

The stock Playwright MCP Docker implementation is documented as **headless Chromium only**. A custom headed image and a display path are therefore required. Xvfb supplies a virtual Linux display, but alone does not show a window on the Mac; a separately protected display viewer or other display transport needs design and testing, including its clipboard and file-transfer surface. Playwright's Docker image is for testing and development and is not recommended for visiting untrusted websites; for scraping it recommends a non-root user and Chromium sandbox with its seccomp profile. Confirm sandbox behavior in the actual Docker Desktop environment. [MCP Docker limitation](https://github.com/microsoft/playwright-mcp/blob/main/README.md), [headed Linux](https://playwright.dev/docs/ci#running-headed), [Playwright Docker security guidance](https://playwright.dev/docs/docker).

Private development targets require an explicit exception to the general private-address deny rule above: authorize a specific hostname, port and approved private CIDR together, never a blanket private-network exemption, and ensure resolution, checked-address connection and upstream TLS identity remain bound. Docker Desktop documents `host.docker.internal` for host services and VPN integration, but neither proves that a particular private DNS zone, VPN, loopback-only host service or address-pinning mechanism works through this proxy. Confirm routing and the target service's listening address without granting the browser a host-network route. Mitmproxy's regular mode supports HTTP/1, HTTP/2 and WebSockets; its documented HTTP/3 support excludes regular mode, so block bypass paths and fail closed for unsupported traffic. The complete setup, policy addon, safe DNS-to-connection binding, protected viewer and private-target access remain **unimplemented and unverified**. [Docker Desktop networking how-tos](https://docs.docker.com/desktop/features/networking/networking-how-tos/), [mitmproxy protocols](https://docs.mitmproxy.org/stable/concepts/protocols/).
