# Behind a proxy: DNS, HTTP/3 and UDP

A proxied Camoufox claims to be a Firefox running on a machine at the proxy's
exit. A server, and the networks on the way to it, see more of that machine
than its address: which resolver asked for the server's name, whether the
ClientHello was encrypted, whether the browser spoke QUIC, and from which
address. This page says what Camoufox does for each, per kind of proxy, and
what the proxy has to do for it to hold.

## The three cases

| | SOCKS5, `proxy_dns="doh"` (default) | SOCKS5, `proxy_dns="exit"` | HTTP proxy |
|---|---|---|---|
| Who resolves a name | the browser, by DoH through the proxy | the proxy | the proxy |
| The proxy is given | addresses | names | names |
| HTTPS records, so real ECH | yes | no (GREASE ECH) | no (GREASE ECH) |
| HTTP/3 and WebTransport | yes, over UDP ASSOCIATE | no | no |
| WebRTC UDP | over UDP ASSOCIATE | over UDP ASSOCIATE | none |
| TLS early data, connection coalescing | as with no proxy | off, as Firefox has them behind any proxy | off |
| `domainLookupStart`/`End` in Resource Timing | a lookup's, as with no proxy | equal: no lookup happened | equal |
| Speculative connections (`<link rel=preconnect>`) | opened, through the proxy | opened, through the proxy | none, as Firefox has behind any proxy |

`proxy_dns` is a launch option of both packages. It applies to the launch
proxy. A context given its own proxy (`NewContext(proxy=...)`,
`new_context(proxy=...)`) is always in the `exit` column: the browser has one
DoH client, which uses the launch proxy, and a context's names must not be
asked through another context's exit. A persistent context
(`persistent_context=True`) has the launch proxy, in the launch's column.

## `doh`: the browser resolves, through the proxy

Stock Firefox resolves names itself and connects to addresses. It asks for the
HTTPS record with the address, and the record is what carries an origin's ECH
configuration and its `alpn=h3`. A browser that hands the proxy a name has no
record: it sends a GREASE ECH extension where stock sends a real one, and it
learns about HTTP/3 only from `Alt-Svc`, one connection late.

With `proxy_dns="doh"` the launcher sets `network.trr.mode` to 3 (DoH only,
no fallback to this host's resolver) and `network.trr.uri` to
`https://mozilla.cloudflare-dns.com/dns-query`, the resolver Firefox's own DoH
rollout uses. Juggler then gives the launch proxy addresses instead of names
(`NetworkObserver.js`), except for the DoH request itself, whose host the exit
resolves. The proxy's own host name is the one name this host's resolver is
asked (`network.trr.excluded-domains`).

The resolver an authoritative DNS server sees is therefore Cloudflare's, asked
from the exit. That is what it sees of a stock Firefox in the countries where
Mozilla has rolled DoH out (the `doh-config` collection of Remote Settings on
2026-11-21: US, CA, RU and UA, all with `cloudflare-global` among the
providers). Elsewhere stock asks the network's own resolver, and a Firefox
user there who turned DoH on is the population this matches. Pass your own
`network.trr.uri` in `firefox_user_prefs` to use another resolver.

The browser now picks the address family, so it has to know whether the exit
has IPv6. When the launch looks the exit up (`geoip`), it also asks an
IPv6-only host through the proxy, and sets `network:ipv4Only` when that fails.

## `exit`: the proxy resolves

The proxy is sent names, over CONNECT and in UDP ASSOCIATE datagrams (WebRTC's
STUN and TURN servers). Nothing is resolved by the browser, and the resolver an
authoritative server sees is the exit's. The cost is the right-hand side of
the table: no HTTPS records, so no real ECH and no HTTP/3, and Resource Timing
shows no lookup. Use it for a proxy that cannot carry the DoH request, or when
the exit's own resolver matters more than the rest.

## Speculative connections

Firefox opens a connection ahead of the request for a `<link rel=preconnect>`,
for a navigation, and for a link as it is pressed. The origin sees that connection arrive, and its TLS handshake, whether or not a
request follows. Firefox opens none of them once a proxy is set, so a page
with one preconnect link to its own origin could tell a proxied browser from
an unproxied one.

Behind a SOCKS proxy Camoufox opens them, through the proxy the request will
use (`zz-proxy-speculative-connect.patch`). In `doh` mode the name is resolved
by DoH and its HTTPS record is used, as for any connection. In `exit` mode the
proxy is sent the name and no record is asked. As in stock, no speculative
connection is opened to an address the browser knows to be private.

Behind an HTTP proxy there are still none: a speculative connection carries no
request, so it would reach the proxy and stop there.

## QUIC through UDP ASSOCIATE

`zz-http3-socks5-udp.patch` sends every datagram of a QUIC connection through a
SOCKS5 UDP ASSOCIATE relay (RFC 1928 §7), on the proxy the request uses. The
association is the one WebRTC uses (`mozilla::net::Socks5UdpAssociation`).

- **One association per QUIC connection**, as each connection has a UDP socket
  of its own without a proxy. A relay that gives each association its own exit
  port shows a server what a NAT shows it.
- **Nothing leaves around the relay.** There is no direct path to fall back to
  and none to migrate to. The handshake starts when the relay is granted, so a
  slow proxy delays it rather than losing its first packets.
- **A proxy without UDP is a network without UDP.** If the proxy refuses the
  association, the connection's datagrams are dropped silently. Firefox then
  does what it does on a network that blocks UDP: the TCP connection it races
  wins, and HTTP/3 is not tried again for that origin for a while.
- **An association lost in use ends its connection.** When the control
  connection closes under a relay that was carrying datagrams, the QUIC
  connection is reset as it is when its socket fails, and its requests are
  sent again over TCP. Left open, it would hold each of them for the 30
  second idle timeout.
- **The server sees stock's packets.** The framing is added below neqo, on the
  socket a direct connection uses, so transport parameters, Initial padding,
  datagram sizes, ECN marks and batching are the ones of a direct connection.
  The relay header (10 bytes for IPv4, 22 for IPv6) is on the hop to the proxy
  only.
- **The hop to the relay may fragment; the datagram is never made smaller.**
  Firefox forbids fragmenting on its QUIC socket and sends 1252 bytes to an
  IPv4 peer, 1232 to an IPv6 one. With the header on, and the relay's own IP
  and UDP headers, that is a packet of 1282 to 1310 bytes, which a 1280-byte
  link (a VPN or a tunnel to the proxy) does not carry. So the socket that
  reaches a relay allows fragmenting, and sends each datagram on its own: a
  segmented send cannot fragment. The relay reassembles and sends the peer the
  datagram a direct browser sends
  ([`relay-hop-may-fragment`](../ci/tribal-rules.yml)).
- **SOCKS5 only.** SOCKS4 and HTTP proxies carry no UDP, and are sent none.

## What the proxy must do

For the claim "a Firefox at the exit" to hold, a SOCKS5 proxy has to:

1. Grant UDP ASSOCIATE, with the same credentials as CONNECT, and keep the
   association for as long as its control connection is open.
2. Send an association's datagrams from the address its CONNECTs exit from.
3. Give each association an exit port of its own and keep it for the
   association's life.
4. Relay datagrams of at least 1500 bytes whole, in both directions, and send
   each to its destination as one unfragmented packet with don't-fragment set,
   as the browser's own socket would. On the hop to the client, accept
   fragments and fragment what does not fit: that hop carries the relay header
   too.
5. Copy the ECN bits of each datagram, in both directions. QUIC marks ECT(0)
   and validates the path; a relay that clears the bits makes every connection
   look like one on an ECN-bleaching path.
6. Accept IPv4 and IPv6 destinations, and relay to IPv6 where the exit has it.
7. Resolve names for `exit` mode and for the DoH request, from the exit.

## Differences from stock that remain

- The DoH requests travel over TCP. Stock tries HTTP/3 first with Cloudflare;
  only the DoH provider can tell.
- In `exit` mode and behind an HTTP proxy, the table's right-hand columns.
- Behind an HTTP proxy, a `<link rel=preconnect>` opens no connection.
- The first hop is 10 or 22 bytes larger per datagram than what the server
  receives, and is fragmented on a link too small for it. A network between
  the browser and the proxy that drops IP fragments loses those datagrams, and
  the browser falls back to TCP.
- Datagrams to a relay are sent one per system call. Nothing past the browser's
  own machine sees that.
- Path MTU discovery is off in Firefox 159 (`network.http.http3.pmtud`). If
  Mozilla turns it on, probes would measure the path through the relay.
