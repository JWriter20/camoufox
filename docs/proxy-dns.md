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

The resolver's timeouts are the ones Firefox has with DoH alone, and the
launcher changes none of them. A lookup waits up to 30 seconds for its answer
(`network.trr.request_timeout_mode_trronly_ms`), longer than a system resolver
waits, so a slow proxy delays a page rather than failing it. A proxy that
refuses the CONNECT to the DoH host leaves no resolver at all: every
navigation fails at once with `NS_ERROR_UNKNOWN_HOST`, and lookups succeed
again once the proxy accepts it. That error on every page means the proxy is
refusing or cannot reach the DoH host, not that the name is wrong
([`doh-mode-keeps-firefox-resolver-timeouts`](../ci/tribal-rules.yml)).

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

The connection is keyed as the request's will be. Firefox partitions a
connection by the site of the page that asked for it, except behind a proxy
(`privacy.partition.network_state.connection_with_proxy`), so the speculative
one is not partitioned either. Keyed apart, it was never used, and a
navigation opened two connections where stock opens one.

Behind an HTTP proxy there are still none: a speculative connection carries no
request, so it would reach the proxy and stop there.

## Optimistic data: the ClientHello behind the CONNECT request

A Firefox with no proxy sends its ClientHello as soon as the TCP handshake is
done: an origin measured it 0.4 to 1.5 ms behind the handshake's last ACK.
Behind a SOCKS proxy the exit does that handshake, tells the browser, and the
browser sends the ClientHello then. The origin sees it one round trip between
the exit and the browser later, 37 to 70 ms in the same measurement, on every
TLS connection.

`proxy_optimistic_data=True` (a launch option of both packages, off by default)
sets `network.proxy.socks5_optimistic_data`. The SOCKS5 layer then reports the
connection made once it has sent the CONNECT request, after the method and
username/password exchange, instead of when the proxy's reply has arrived
(`zz-socks5-optimistic-data.patch`). TLS starts at once, so the ClientHello
is on its way to the proxy before the proxy has connected, and a proxy that
keeps it can send it the moment its own handshake with the destination is
done. The proxy's reply is read, and consumed, by the SOCKS5 layer when TLS
first reads; what follows it is the ServerHello.

- **Only a TLS connection's first flight.** A connection with no TLS layer
  (plain HTTP) waits for the reply as before, as does SOCKS4, an HTTP proxy,
  and a UDP ASSOCIATE. A speculative connection behind a SOCKS5 proxy is a TLS
  connection like any other.
- **The origin receives the bytes it receives today.** The ClientHello is
  NSS's, written as NSS writes it with no proxy. On the hop to the proxy the
  request and the ClientHello are separate writes on a socket with Nagle's
  algorithm off, so separate segments. The origin sees neither: the exit's own
  TCP stack segments what the exit writes.
- **A refused CONNECT is the error it is without the option**
  (`NS_ERROR_CONNECTION_REFUSED` and the others of RFC 1928 §6). The bytes
  sent ahead went nowhere. It is asked fewer times, though: a refusal met
  while connecting is retried by the connection attempt itself, and this one
  arrives after it (one navigation to a refusing destination: 5 CONNECTs
  without the option, 3 with).
- **The connection counts as made earlier.** Resource Timing's `connectEnd`
  is when the request was sent, and the exit's handshake with the destination
  is inside the TLS time that follows. What runs only while connecting stops
  earlier too: the backup connection Firefox opens after 250 ms without one
  (`network.http.connection-retry-timeout`), the 90 second connect timeout
  (`network.http.connection-timeout`), and trying a name's next address after
  a refusal. A destination that never answers is given up when the proxy
  says so or when the TLS handshake times out
  (`network.http.tls-handshake-timeout`, 30 seconds).
- **It applies to every SOCKS5 proxy of the browser**, a context's own too:
  it is a pref, not a property of one proxy.

Turn it on only for a proxy that is known to forward those bytes, on every
hop. A proxy that discards what it received before its reply answers the
CONNECT, and the handshake then waits for a ServerHello that cannot come.
Every HTTPS navigation hangs: each handshake waits out
`network.http.tls-handshake-timeout` (30 seconds) and Firefox opens another,
as it does with a server that never answers. Measured, one navigation had not
failed after 200 seconds and six connections; with the timeout set to 5
seconds it failed with `NS_ERROR_NET_TIMEOUT` after 25 seconds or more and
two. A navigation with no proxy, to an origin that never answers a
ClientHello, had not failed after 60. The
browser cannot tell a lost ClientHello from a slow server, and does not retry
without the option: that retry would be one more connection the origin sees
([`optimistic-data-is-opt-in-and-tls-only`](../ci/tribal-rules.yml)).

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
- **TCP is raced when a Firefox at the exit would race it.** Firefox gives a
  QUIC connection a head start before it also opens a TCP one: 50 ms when
  HTTP/3 is known from an HTTPS record
  (`network.dns.httpssvc.http3_fast_fallback_timeout`), 100 ms from Alt-Svc
  (`network.http.http3.backup_timer_delay`). Whichever is ready first carries
  the request, and an origin that answers QUIC in time sees no TCP connection
  at all. Behind the proxy QUIC's first datagram waits for the association,
  and its answer crosses the proxy, so a timer that runs out starts again for
  what that cost: the time the relay took to set up, and one round trip to
  the proxy, measured on the UDP ASSOCIATE request. Without that the origin
  got a TCP connection stock never opens, and the request went over it
  whenever TCP won, which `proxy_optimistic_data` made the rule for a proxy
  more than 50 ms away
  ([`http3-is-raced-on-the-exits-clock`](../ci/tribal-rules.yml)).
- **A request held behind a stylesheet is raced on the exit's clock too.** A
  page's render-blocking requests hold its other ones back, and the 50 ms
  timer of a held request runs meanwhile. With speculative connections off,
  which is uBlock Origin's default, nothing has connected for it when the
  timer runs out. Behind the proxy the timer then waits for what the proxy
  cost the requests it is held behind: their relay's setup and two round
  trips to the proxy, a handshake and a response. A request let go by then
  waits for its own relay, as above. One still held starts TCP, as it does
  at the exit. With `proxy_optimistic_data` that connection's ClientHello
  reaches the origin a round trip to the proxy sooner than the first
  datagram of the QUIC connection it races, which waits for its relay, so
  once ready it waits that round trip before it takes the request. Without
  this such a page's fonts, fetches and EventSources went over HTTP/2 on new
  TCP connections where a browser with no proxy sends them over HTTP/3.
- **A connection closed before its relay is ready sends nothing.** All it
  has to say by then is that it closed, in the first datagram of a
  connection its peer never saw.
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
8. For `proxy_optimistic_data` only: keep every byte that arrives on a CONNECT's
   connection after the request, send it to the destination as soon as the
   destination is connected, in the order received and as one write, and only
   then reply. A chain of proxies has to do this on every hop.

## Differences from stock that remain

- The DoH requests travel over TCP. Stock tries HTTP/3 first with Cloudflare;
  only the DoH provider can tell.
- In `exit` mode and behind an HTTP proxy, the table's right-hand columns.
- Without `proxy_optimistic_data`, a TLS connection's ClientHello reaches the
  origin a round trip between the exit and the browser after the TCP
  handshake. With it, `connectEnd` no longer marks the end of that handshake.
- Behind an HTTP proxy, a `<link rel=preconnect>` opens no connection.
- The first hop is 10 or 22 bytes larger per datagram than what the server
  receives, and is fragmented on a link too small for it. A network between
  the browser and the proxy that drops IP fragments loses those datagrams, and
  the browser falls back to TCP.
- A TCP connection raced against QUIC starts later than it does at the exit,
  by the delay of the proxy. An origin whose QUIC is slow or blocked sees the
  TCP connection that much later.
- Timers inside QUIC and TLS run on the round trip of the whole path, the
  proxy's included. An origin whose record offers an ECH configuration it
  rejects sees more TCP connections than it does from its exit: the QUIC
  connection takes three probe timeouts to close, and TCP is raced meanwhile.
- Datagrams to a relay are sent one per system call. Nothing past the browser's
  own machine sees that.
- Path MTU discovery is off in Firefox 159 (`network.http.http3.pmtud`). If
  Mozilla turns it on, probes would measure the path through the relay.
